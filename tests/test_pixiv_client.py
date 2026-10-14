"""pixiv_client（插画/漫画）的测试。

**形状一律按真实响应来**：详情顶层是 `{"illust": {...}}`、多页在 `meta_pages` 里、
图 URL 带 CDN 缩放段 `/c/540x540_70/`。理由见参照仓踩过的坑 —— pixivpy3 的
`JsonDict` 对缺失的键返回 `None` 而**不抛异常**，手写「字段全在顶层」的假数据
会让结构性 bug 全绿通过（那个 bug 在小说插件里真发生过）。
"""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace
from typing import ClassVar

import pytest
from conftest import wrap_json
from nonebot_plugin_pixiv_illust import pixiv_client as pc
from PIL import Image, ImageFilter
from pixivpy3 import PixivError

# ── 真实 URL 形状 ──────────────────────────────────────────────────
# App `image_urls.large` / `meta_pages[*].image_urls.large` 实测就是这种：
# 带 `/c/540x540_70/` 缩放段、结尾 `_master1200.jpg`；**去掉缩放段才是原图**。
SCALED_P0 = "https://i.pximg.net/c/540x540_70/img-master/img/2026/01/02/03/04/05/111_p0_master1200.jpg"
ORIGINAL_P0 = "https://i.pximg.net/img-master/img/2026/01/02/03/04/05/111_p0_master1200.jpg"

# 生产日志里那条真实错误体（HTTP 200 + 只有 error 键）—— 鉴权过期时的形态
_PROD_AUTH_ERROR = {
    "error": {
        "user_message": "",
        "message": (
            "Error occurred at the OAuth process. Please check your Access Token"
            " to fix this. Error Message: invalid_grant"
        ),
        "reason": "",
        "user_message_details": {},
    }
}


def _scaled(page: int) -> str:
    return f"https://i.pximg.net/c/540x540_70/img-master/img/2026/01/02/03/04/05/111_p{page}_master1200.jpg"


def _png_bytes(size=(640, 1216), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _illust_payload(
    illust_id: int = 111,
    *,
    pages: int = 1,
    x_restrict: int = 0,
    title: str = "测试插画",
    author: str = "画师甲",
) -> dict:
    """作品详情的**真实形状**：单页时 `meta_pages=[]`，多页才有条目。

    ⚠️ 所有图 URL 都带 `/c/540x540_70/` 缩放段（App API 实测如此）——
    手写的「干净 URL」会让 strip_scale 的接线测不出来。
    """
    return {
        "id": illust_id,
        "title": title,
        "x_restrict": x_restrict,
        "page_count": max(pages, 1),
        "image_urls": {"large": _scaled(0), "medium": _scaled(0)},
        "meta_pages": ([{"image_urls": {"large": _scaled(p)}} for p in range(pages)] if pages > 1 else []),
        "user": {"id": 61943687, "name": author, "account": "painter"},
    }


class _FakeHttp:
    """替掉 httpx.AsyncClient：记录被请求的 URL，返回固定字节。"""

    def __init__(self, content: bytes, *, fail: bool = False):
        self._content = content
        self._fail = fail

    def install(self, monkeypatch):
        content, fail = self._content, self._fail
        calls: list[str] = []
        self.calls = calls

        class _C:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                calls.append(url)
                if fail:
                    raise OSError("connection reset by peer")

                class R:
                    pass

                r = R()
                r.content = content
                r.raise_for_status = lambda: None
                return r

        monkeypatch.setattr(pc.httpx, "AsyncClient", _C)


# ══════════════════════════════════════════════════════════════════
# build_api：参数名必须是 `proxies`（dict），不是 `proxy`
# ══════════════════════════════════════════════════════════════════


def test_build_api_uses_proxies_dict(monkeypatch):
    """回归：requests/pixivpy3 的参数名是 `proxies`（dict），不是 `proxy`。

    写成 `proxy=` **不报错但静默失效** —— 那样所有 pixiv 请求都会走直连，
    而 pixiv 在国内直连不通，表现为「插件完全没反应」。
    """
    captured = {}

    class FakeAPI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def auth(self, refresh_token):
            captured["_token"] = refresh_token

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    pc.build_api("tok", "http://127.0.0.1:1081")

    assert captured["proxies"] == {
        "http": "http://127.0.0.1:1081",
        "https": "http://127.0.0.1:1081",
    }
    assert "proxy" not in captured  # 不能是单数写法
    assert captured["_token"] == "tok"


def test_build_api_omits_proxies_when_empty(monkeypatch):
    """proxy 为空 = 直连，这时**不能**传空的 proxies dict。"""
    captured = {}

    class FakeAPI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def auth(self, refresh_token):
            pass

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    pc.build_api("tok", "")
    assert "proxies" not in captured


# ══════════════════════════════════════════════════════════════════
# strip_scale：CDN 缩放段 → 原图
# ══════════════════════════════════════════════════════════════════


def test_strip_scale_removes_cdn_segment():
    url = "https://i.pximg.net/c/540x540_70/img-master/img/2026/01/02/03/04/05/111_p0_master1200.jpg"
    assert pc.strip_scale(url) == "https://i.pximg.net/img-master/img/2026/01/02/03/04/05/111_p0_master1200.jpg"


def test_strip_scale_handles_every_scale_form():
    """CDN 缩放段有带质量后缀的（`_70`/`_80`）也有不带的，两种都要认。"""
    for spec in ("/c/540x540_70/", "/c/240x480_80/", "/c/600x600/", "/c/1200x1200/"):
        assert pc.strip_scale(SCALED_P0.replace("/c/540x540_70/", spec)) == ORIGINAL_P0


def test_strip_scale_leaves_square_images_alone():
    """⚠️ `_square1200` 是**裁切过的方图**：去掉 /c/ 段拿到 1200x1200 方图，不是插画比例。

    实测过（小说插件的封面同款问题），所以这类必须原样返回，别去动它。
    """
    square = "https://i.pximg.net/c/128x128/img-master/img/2026/01/02/03/04/05/111_p0_square1200.jpg"
    assert pc.strip_scale(square) == square


def test_strip_scale_is_idempotent_and_tolerates_odd_input():
    """poller 与 URL hook 两条链路都可能调 → 必须幂等；空串/无缩放段要安全。"""
    once = pc.strip_scale(SCALED_P0)
    assert pc.strip_scale(once) == once
    assert pc.strip_scale("") == ""
    assert pc.strip_scale("https://example.com/a.jpg") == "https://example.com/a.jpg"
    # 头像 URL 没有 /c/ 段，套用无副作用
    avatar = "https://i.pximg.net/user-profile/img/2026/03/09/23/11/38/28616698_x_170.jpg"
    assert pc.strip_scale(avatar) == avatar


# ══════════════════════════════════════════════════════════════════
# page_urls：多页走 meta_pages，单页退化 image_urls.large
# ══════════════════════════════════════════════════════════════════


def test_page_urls_multipage_prefers_meta_pages():
    class IU:
        large = "https://i.pximg.net/c/540x540_70/x/1_p0_master1200.jpg"

    class MP:
        image_urls = IU()

    class It:
        meta_pages: ClassVar[list] = [MP(), MP()]  # 真实形状：多页是一串条目（ClassVar 仅为过 ruff RUF012）
        image_urls = IU()

    assert len(pc.page_urls(It())) == 2


def test_page_urls_single_page_falls_back_to_image_urls():
    class IU:
        large = "https://i.pximg.net/c/540x540_70/x/1_p0_master1200.jpg"

    class It:
        meta_pages: ClassVar[list] = []  # 单页时 meta_pages 是空 list（不是 None）
        image_urls = IU()

    assert pc.page_urls(It()) == ["https://i.pximg.net/c/540x540_70/x/1_p0_master1200.jpg"]


def test_page_urls_keeps_raw_urls_in_page_order():
    """⚠️ 返回的是**原始 URL**（带缩放段）——strip_scale 是采集端的事，页序不能乱。"""
    urls = pc.page_urls(wrap_json(_illust_payload(pages=3)))
    assert urls == [_scaled(0), _scaled(1), _scaled(2)]
    assert all("/c/540x540_70/" in u for u in urls)


def test_page_urls_skips_empty_entries_and_tolerates_missing_meta_pages():
    """meta_pages 里混着空 URL、或者整个字段缺失（老响应）时都不能炸。"""
    mixed = SimpleNamespace(
        meta_pages=[
            SimpleNamespace(image_urls=SimpleNamespace(large="")),
            SimpleNamespace(image_urls=None),
            SimpleNamespace(image_urls=SimpleNamespace(large=_scaled(1))),
        ],
        image_urls=SimpleNamespace(large=_scaled(0)),
    )
    assert pc.page_urls(mixed) == [_scaled(1)]  # meta_pages 有内容就只用它

    no_meta = SimpleNamespace(image_urls=SimpleNamespace(large=_scaled(0)))  # 没有 meta_pages 属性
    assert pc.page_urls(no_meta) == [_scaled(0)]

    empty = SimpleNamespace(meta_pages=[], image_urls=SimpleNamespace(large=""))
    assert pc.page_urls(empty) == []


# ══════════════════════════════════════════════════════════════════
# user_illusts：illust + manga 两次取、按 id 去重升序
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_user_illusts_merges_illust_and_manga_dedup_sorted(monkeypatch):
    """⚠️ `type='illust'` **不包含**漫画，必须再取一次 `type='manga'` 合并。"""
    calls = []

    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def user_illusts(self, author_id, type="illust"):
            calls.append((author_id, type))
            if type == "illust":
                return wrap_json({"illusts": [{"id": 30}, {"id": 10}]})
            return wrap_json({"illusts": [{"id": 20}, {"id": 10}]})  # 10 是两条都返回过的

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    got = await pc.PixivClient("tok", "").user_illusts(61943687)

    assert calls == [(61943687, "illust"), (61943687, "manga")]
    assert [int(it.id) for it in got] == [10, 20, 30]  # 去重 + 升序（高水位语义依赖它）


@pytest.mark.asyncio
async def test_user_illusts_reauths_on_error_payload(monkeypatch):
    """鉴权一过期就整个轮询白跑 —— 订阅链路必须能自愈（丢弃实例、重新 auth）。"""
    monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
    made = []

    class FakeAPI:
        def __init__(self, **kwargs):
            made.append(self)

        def auth(self, refresh_token):
            pass

        def user_illusts(self, author_id, type="illust"):
            if len(made) == 1:  # 第 1 个实例 = 已经过期的那一个
                return wrap_json(_PROD_AUTH_ERROR)
            return wrap_json({"illusts": [{"id": 7}]})

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    got = await pc.PixivClient("tok", "").user_illusts(1)

    assert len(made) == 2  # 必须丢弃过期实例并重建鉴权
    assert [int(it.id) for it in got] == [7]


@pytest.mark.asyncio
async def test_user_illusts_raises_instead_of_silently_returning_empty(monkeypatch):
    """不能「静默返回空列表」—— 那会让轮询把一轮新作永久跳过。"""
    monkeypatch.setattr(pc.time, "sleep", lambda _s: None)

    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def user_illusts(self, author_id, type="illust"):
            return wrap_json(_PROD_AUTH_ERROR)

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    with pytest.raises(PixivError):
        await pc.PixivClient("tok", "").user_illusts(1)


# ══════════════════════════════════════════════════════════════════
# illust_detail：解包 {"illust": {...}} + 结构体检
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_illust_detail_unwraps_the_illust_envelope(monkeypatch):
    """接口顶层是 `{"illust": {...}}`，**必须解包**，否则字段全拿不到（见下条测试）。"""

    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def illust_detail(self, illust_id):
            return wrap_json({"illust": _illust_payload(illust_id, pages=2, x_restrict=1)})

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    detail = await pc.PixivClient("tok", "").illust_detail(555)

    assert int(detail.id) == 555
    assert detail.x_restrict == 1  # R18 判定
    assert detail.title == "测试插画"
    assert detail.user.name == "画师甲"  # URL hook 直接读它（None 就是 AttributeError）
    assert len(pc.page_urls(detail)) == 2  # 多页


def test_raw_illust_response_loses_all_fields_without_unwrapping():
    """**核心回归**：不解包时字段全取不到 —— 这就是那个真 bug 的样子。

    注意 `getattr` 的默认值 0 **不起作用**（JsonDict 让键「存在但为 None」）。
    """
    raw = wrap_json({"illust": _illust_payload(pages=2, x_restrict=1)})
    assert raw.x_restrict is None  # 不是 0！
    assert getattr(raw, "x_restrict", 0) is None  # 默认值也救不了
    assert raw.title is None
    assert raw.meta_pages is None
    assert raw.user is None
    assert int(raw.x_restrict or 0) == 0  # → R18 判定会退化成 0

    unwrapped = pc.unwrap_illust_shape(raw)
    assert unwrapped.x_restrict == 1
    assert [str(u) for u in unwrapped.meta_pages]  # 多页数据在解包后才可见


def test_unwrap_illust_shape_is_idempotent_for_already_unwrapped_input():
    inner = wrap_json(_illust_payload())
    assert pc.unwrap_illust_shape(inner) is inner
    assert pc.unwrap_illust_shape("别的类型") == "别的类型"


@pytest.mark.asyncio
async def test_illust_detail_reauths_and_recovers_from_expired_token(monkeypatch):
    """★ 核心回归：静默 error 载荷 ⇒ 丢弃旧实例、重新鉴权、取回作品。"""
    monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
    made = []

    class FakeAPI:
        def __init__(self, **kwargs):
            made.append(self)
            self.auth_calls = 0

        def auth(self, refresh_token):
            self.auth_calls += 1

        def illust_detail(self, illust_id):
            if len(made) == 1:  # 第 1 个实例 = 已经过期的那一个
                return wrap_json(_PROD_AUTH_ERROR)
            return wrap_json({"illust": _illust_payload(illust_id, x_restrict=1)})

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    detail = await pc.PixivClient("tok", "").illust_detail(29275792)

    assert len(made) == 2, "必须丢弃过期实例并重建鉴权"
    assert made[1].auth_calls == 1, "新实例要真的做过 auth()"
    assert detail.x_restrict == 1, "重新鉴权后必须真的取到作品（R18 判定可用）"
    assert detail.user.name, "user 不能再是 None（那是 AttributeError 的来源）"


@pytest.mark.asyncio
async def test_error_payload_raises(monkeypatch):
    """resp 含顶层 `error` 时必须**抛**，不许静默返回（否则调用方把错误当数据发出去）。"""
    monkeypatch.setattr(pc.time, "sleep", lambda _s: None)

    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def illust_detail(self, illust_id):
            return wrap_json(_PROD_AUTH_ERROR)

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    with pytest.raises(PixivError, match="invalid_grant"):
        await pc.PixivClient("tok", "").illust_detail(29275792)


def test_is_error_payload_recognizes_the_real_production_error():
    """必须认出生产那条「HTTP 200 + error 体」—— 这是修鉴权过期 bug 的前提。"""
    assert pc.is_error_payload(wrap_json(_PROD_AUTH_ERROR))


def test_is_error_payload_does_not_misfire_on_real_success_shapes():
    """真实成功响应不能被误判成错误 —— 误判会让正常作品全部「取失败」。"""
    assert not pc.is_error_payload(wrap_json({"illust": _illust_payload()}))
    assert not pc.is_error_payload(wrap_json({"illusts": [{"id": 1}], "next_url": None}))
    assert not pc.is_error_payload(wrap_json({"id": 1, "title": "t"}))


@pytest.mark.parametrize("value", [None, "", "error", [], 0])
def test_is_error_payload_ignores_non_dict(value):
    assert not pc.is_error_payload(value)


def test_check_illust_shape_warns_when_pages_field_missing(caplog):
    """结构变了要**大声告警**（丢 meta_pages = 多页作品静默只发第 1 页），别静默降级。"""
    bad = wrap_json(
        {"illust": {"id": 1, "title": "t", "x_restrict": 0, "image_urls": {"large": "u"}, "user": {"name": "a"}}}
    )
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_pixiv_illust"):
        pc._check_illust_shape(bad["illust"])
    assert "缺少字段" in caplog.text
    assert "meta_pages" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_pixiv_illust"):
        pc._check_illust_shape(wrap_json(_illust_payload()))  # 正常形状（单页 meta_pages=[]）
    assert caplog.text == ""


def test_check_illust_shape_accepts_x_restrict_zero_and_empty_meta_pages(caplog):
    """`x_restrict=0` 与 `meta_pages=[]` 都是**合法值**，不能被当成「缺字段」。"""
    illust = wrap_json(_illust_payload(x_restrict=0, pages=1))
    assert illust.meta_pages == []
    with caplog.at_level(logging.WARNING, logger="nonebot_plugin_pixiv_illust"):
        pc._check_illust_shape(illust)
    assert caplog.text == ""


# ══════════════════════════════════════════════════════════════════
# 鉴权 TTL（token 只有 1 小时寿命，必须主动换）
# ══════════════════════════════════════════════════════════════════


def test_auth_ttl_sits_below_the_one_hour_token_lifetime():
    """TTL 必须 < 1 小时（留余量），也不能过短（否则每次调用都白换一次 token）。"""
    assert 10 * 60 <= pc._AUTH_TTL_SECONDS < 60 * 60


def test_get_api_rebuilds_only_after_ttl(monkeypatch):
    """TTL 到期**主动**重建鉴权；TTL 内复用同一实例（不白换 token）。"""
    monkeypatch.setattr(pc, "_AUTH_TTL_SECONDS", 100.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(pc.time, "monotonic", lambda: clock["t"])
    made = []

    class FakeAPI:
        def __init__(self, **kwargs):
            made.append(self)

        def auth(self, refresh_token):
            pass

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    client = pc.PixivClient("tok", "")

    client._get_api()
    client._get_api()
    assert len(made) == 1, "TTL 内应复用，不该重复鉴权"

    clock["t"] += 99
    client._get_api()
    assert len(made) == 1, "还没到 TTL 就不该重建"

    clock["t"] += 2  # 累计 101s > 100s
    client._get_api()
    assert len(made) == 2, "过了 TTL 必须重建鉴权（这正是原 bug 的正面修复）"


# ══════════════════════════════════════════════════════════════════
# blur_image / process_image：固定 px 半径 + JPEG
# ══════════════════════════════════════════════════════════════════


def test_blur_image_uses_given_radius_verbatim(monkeypatch):
    """需求：模糊半径是**固定像素**（6–12px），**不随图片尺寸缩放**。"""
    captured = []
    real_gaussian = ImageFilter.GaussianBlur

    def spy(radius=0):
        captured.append(radius)
        return real_gaussian(radius)

    monkeypatch.setattr(pc.ImageFilter, "GaussianBlur", spy)
    # 同一个 radius 作用在尺寸悬殊的两张图上，传进 Pillow 的值必须完全一样
    pc.blur_image(_png_bytes(size=(100, 200)), radius=9)
    pc.blur_image(_png_bytes(size=(640, 1216)), radius=9)
    assert captured == [9, 9]


def test_blur_radius_bounds_are_passed_through(monkeypatch):
    """需求边界 6 与 12 必须原样传给 Pillow（不被缩放/四舍五入）。"""
    captured = []
    real_gaussian = ImageFilter.GaussianBlur

    def spy(radius=0):
        captured.append(radius)
        return real_gaussian(radius)

    monkeypatch.setattr(pc.ImageFilter, "GaussianBlur", spy)
    pc.blur_image(_png_bytes(), radius=6)
    pc.blur_image(_png_bytes(), radius=12)
    assert captured == [6, 12]


def test_blur_image_changes_pixels_and_stays_decodable():
    out = pc.blur_image(_png_bytes(), radius=12)
    assert out != _png_bytes()
    assert out.startswith(b"\xff\xd8")  # JPEG
    assert Image.open(io.BytesIO(out)).size == (640, 1216)


def test_process_image_returns_bytes_untouched_when_noop():
    """max_width=0 且不模糊 → 原样返回（不重编码，省 CPU 也不掉质量）。"""
    src = _png_bytes()
    assert pc.process_image(src, blur=False, radius=9, max_width=0) == src


def test_process_image_resizes_keeping_ratio_and_never_upscales():
    out = pc.process_image(_png_bytes(size=(901, 1200)), blur=False, radius=9, max_width=800)
    assert pc.image_size(out) == (800, round(1200 * 800 / 901))

    small = _png_bytes(size=(400, 600))
    assert pc.process_image(small, blur=False, radius=9, max_width=800) == small


def test_process_image_scales_before_blurring_not_after():
    """⚠️ 顺序断言：**先缩到 800、再模糊 9px**（半径是「最终那张图」上的固定像素）。

    做法：与手工「先缩后模糊」的结果逐字节比 —— 顺序写反了这里必红。
    """
    src = _png_bytes(size=(901, 1200), color=(180, 80, 40))
    out = pc.process_image(src, blur=True, radius=9, max_width=800)

    img = Image.open(io.BytesIO(src)).convert("RGB")
    resized = img.resize((800, round(1200 * 800 / 901)), Image.Resampling.LANCZOS)
    manual = resized.filter(ImageFilter.GaussianBlur(radius=9))
    buf = io.BytesIO()
    manual.save(buf, format="JPEG", quality=88)
    assert out == buf.getvalue()


def test_process_image_converts_palette_images_before_jpeg():
    """调色板/带 alpha 的 PNG 直接存 JPEG 会炸 —— 必须先转 RGB。"""
    buf = io.BytesIO()
    Image.new("P", (640, 900)).save(buf, format="PNG")
    out = pc.process_image(buf.getvalue(), blur=True, radius=9, max_width=0)
    assert out.startswith(b"\xff\xd8")


# ══════════════════════════════════════════════════════════════════
# fetch_image / download_image
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_image_sends_referer_and_proxy(monkeypatch):
    """页图/头像都必须带 Referer，否则 i.pximg.net 返回 403。"""
    captured = {}

    class FakeResp:
        content = _png_bytes()

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr(pc.httpx, "AsyncClient", FakeClient)
    out = await pc.PixivClient("tok", "http://p:1").fetch_image("https://i.pximg.net/x.jpg")

    assert captured["headers"]["Referer"] == "https://www.pixiv.net/"
    assert captured["proxy"] == "http://p:1"
    assert out == _png_bytes()  # fetch_image 本身不处理，原样返回


@pytest.mark.asyncio
async def test_download_image_strips_scale_before_fetch(monkeypatch):
    """⚠️ 拿到的必须是**原图 URL**（无 /c/ 段）—— 忘了 strip 会一直下缩略图（且静默）。"""
    http = _FakeHttp(_png_bytes(size=(1200, 1600)))
    http.install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(SCALED_P0, blur=False, radius=9, max_width=0)

    assert http.calls == [ORIGINAL_P0]
    assert out == _png_bytes(size=(1200, 1600))  # 不模糊不缩放 → 原样返回


@pytest.mark.asyncio
async def test_download_image_skips_network_for_placeholder_url(monkeypatch):
    """占位图 URL **不该发起下载** —— 省一次无用的网络请求（经代理很贵）。"""
    http = _FakeHttp(_png_bytes(size=(640, 900)))
    http.install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(
        "https://s.pximg.net/common/images/limit_unknown_100.png", blur=False, radius=9
    )
    assert out == b""
    assert http.calls == []


@pytest.mark.asyncio
async def test_download_image_drops_downloaded_placeholder(monkeypatch):
    """URL 没特征、但下到的图是 100x100 → 也得丢掉（字节层面兜底）。"""
    http = _FakeHttp(_png_bytes(size=(100, 100)))
    http.install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(
        "https://i.pximg.net/c/600x600/img-master/img/x_p0_master1200.jpg",
        blur=False,
        radius=9,
    )
    assert out == b""


@pytest.mark.asyncio
async def test_download_image_applies_blur_and_keeps_size(monkeypatch):
    big = _png_bytes(size=(640, 1216))
    http = _FakeHttp(big)
    http.install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(ORIGINAL_P0, blur=True, radius=9)
    assert out != big  # 模糊生效
    assert Image.open(io.BytesIO(out)).size == (640, 1216)  # 尺寸不变


@pytest.mark.asyncio
async def test_download_image_can_skip_blur(monkeypatch):
    # ⚠️ 图必须**够大**（> 160px）：小图会被「占位图检测」当成 limit_unknown 丢掉。
    big = _png_bytes(size=(640, 900))
    _FakeHttp(big).install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(ORIGINAL_P0, blur=False, radius=9)
    assert out == big  # 既不模糊也不缩放 → 原样返回（不重编码）


@pytest.mark.asyncio
async def test_download_image_passes_max_width_down(monkeypatch):
    _FakeHttp(_png_bytes(size=(901, 1200))).install(monkeypatch)

    out = await pc.PixivClient("tok", "").download_image(ORIGINAL_P0, blur=False, radius=9, max_width=600)
    assert pc.image_size(out) == (600, round(1200 * 600 / 901))


@pytest.mark.asyncio
async def test_download_image_empty_url_returns_empty_bytes():
    """空 URL 直接返回空，**不抛异常** —— 调用方按「这页没图」处理。"""
    assert await pc.PixivClient("tok", "").download_image("", blur=True, radius=9) == b""


# ══════════════════════════════════════════════════════════════════
# author_info：订阅时顺手存画师名/头像（取不到不能拖垮订阅）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_author_info_reads_name_and_avatar(monkeypatch):
    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def user_illusts(self, author_id, type="illust"):
            payload = _illust_payload()
            payload["user"]["profile_image_urls"] = {"medium": "https://i.pximg.net/avatar.jpg"}
            return wrap_json({"user": payload["user"], "illusts": [payload]})

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    name, avatar = await pc.PixivClient("tok", "").author_info(1)
    assert (name, avatar) == ("画师甲", "https://i.pximg.net/avatar.jpg")


@pytest.mark.asyncio
async def test_author_info_falls_back_to_first_illust_user_and_survives_failure(monkeypatch):
    """顶层没有 user 时退到首条作品的 user；整条链路失败 → `("", "")`，**不抛异常**。"""
    monkeypatch.setattr(pc.time, "sleep", lambda _s: None)  # 别真等重试间隔

    class FakeAPI:
        def __init__(self, **kwargs):
            pass

        def auth(self, refresh_token):
            pass

        def user_illusts(self, author_id, type="illust"):
            if author_id == 2:
                raise RuntimeError("api down")
            return wrap_json({"illusts": [wrap_json(_illust_payload())]})

    monkeypatch.setattr(pc, "AppPixivAPI", FakeAPI)
    client = pc.PixivClient("tok", "")
    assert await client.author_info(1) == ("画师甲", "")
    assert await client.author_info(2) == ("", "")
