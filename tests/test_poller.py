"""poller 的高水位语义测试。

7 条硬语义（与参照仓 pixiv 小说插件逐条对齐，**一条不许改**）：
  ① 首轮 `last_seen == 0` 只播种不推送；
  ② 新作按 ID 升序推、受 `max_push_per_poll` 限制、`landed` 之外不动高水位；
  ③ `_is_pushable` 显式判 `is False` / `is not True`（缺字段照推，不许用 bool()）；
  ④ R18 且 `r18_push_enabled=False` → 跳过但高水位跨过去；
  ⑤ 群被白/黑名单排除 → 不推且高水位直接推到最新；
  ⑥ `landed` 到 `max_push_per_poll` 就 break、剩下的留给下一轮；
  ⑦ 单作者拉取异常不拖垮整轮；发送失败也记 landed。

Fake 类**必须复刻真实形状**：多页在 `meta_pages[*].image_urls.large`、单页 `meta_pages=[]`
退化到 `image_urls.large`，且 URL 带 CDN 缩放段 `/c/540x540_70/` —— 写成「干净 URL」
会让「poller 有没有 strip 缩放段取原图」的接线断言**假通过**。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pytest
from conftest import wrap_json
from nonebot_plugin_pixiv_illust import poller, store
from nonebot_plugin_pixiv_illust.config import Config
from PIL import Image


def _scaled(page: int) -> str:
    """App API 给的页图就是这种：**带 /c/540x540_70/ 缩放段**。"""
    return f"https://i.pximg.net/c/540x540_70/img-master/img/2026/10/14/12/00/00/111_p{page}_master1200.jpg"


def _original(page: int) -> str:
    """去掉缩放段才是原图（master1200 档）。"""
    return f"https://i.pximg.net/img-master/img/2026/10/14/12/00/00/111_p{page}_master1200.jpg"


def _real_png_bytes(size=(640, 1216), color=(200, 60, 60)) -> bytes:
    """真正可解码的图片字节（全链路测试用：客户端会对它跑 PIL 缩放/模糊）。"""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class FakeTag:
    name: str


@dataclass
class FakeUser:
    id: int = 200
    name: str = "画师甲"


@dataclass
class FakeImageUrls:
    # ⚠️ 必须带 `/c/540x540_70/`（App 实测形状）
    large: str = field(default_factory=lambda: _scaled(0))


@dataclass
class FakeMetaPage:
    image_urls: FakeImageUrls = field(default_factory=FakeImageUrls)


@dataclass
class FakeIllust:
    id: int
    title: str = "插画"
    x_restrict: int = 0
    page_count: int = 1
    tags: list = field(default_factory=list)
    user: FakeUser = field(default_factory=FakeUser)
    visible: bool = True
    is_mypixiv_only: bool = False
    image_urls: FakeImageUrls = field(default_factory=FakeImageUrls)
    # 单页作品 `meta_pages=[]`（真实形状）；多页用 _multi_page() 构造
    meta_pages: list = field(default_factory=list)


def _multi_page(illust_id: int, pages: int, **kw) -> FakeIllust:
    """多页插画/漫画：meta_pages 每页一张，**每张都带 CDN 缩放段**。"""
    return FakeIllust(
        illust_id,
        page_count=pages,
        meta_pages=[FakeMetaPage(FakeImageUrls(_scaled(p))) for p in range(pages)],
        **kw,
    )


class FakeClient:
    """假 client，方法形状与 `PixivClient` 一致：user_illusts / illust_detail / download_image。

    `download_image` 每次返回**可区分**的字节（page-1、page-2…），
    这样「第 1 页随文案发 / 第 2..N 页交给 send_pages」才测得出来。
    """

    def __init__(self, illusts_by_author, detail_map=None):
        self.illusts_by_author = illusts_by_author
        self.detail_map = detail_map or {}
        self.download_calls: list[tuple[str, bool, int]] = []  # (url, blur, radius)
        self.max_widths: list[int] = []  # 单独一列，别动上面三元组解包的老断言
        self._n = 0

    async def user_illusts(self, author_id):
        return self.illusts_by_author.get(author_id, [])

    async def illust_detail(self, illust_id):
        return self.detail_map[illust_id]

    async def download_image(self, url, *, blur, radius, max_width=0):
        self.download_calls.append((url, blur, radius))
        self.max_widths.append(max_width)
        self._n += 1
        return f"page-{self._n}".encode()


@pytest.fixture(autouse=True)
def _db(tmp_path):
    store.init(tmp_path / "t.sqlite3")
    yield


async def _noop_send(group_id, message):
    pass


async def _noop_send_pages(group_id, pages):
    pass


def _cfg(**kw):
    base = {
        "pixiv_illust_max_push_per_poll": 5,
        "pixiv_illust_max_pages": 4,
        "pixiv_illust_image_max_width": 0,
        "pixiv_illust_blur_r18": True,
        "pixiv_illust_blur_radius": 9,
        "pixiv_illust_r18_push_enabled": True,
    }
    base.update(kw)
    return Config(**base)


def _image_count(msg) -> int:
    return len([seg for seg in msg if seg.type == "image"])


# ══════════════════════════════════════════════════════════════════
# ① 首轮只播种；② 升序 + 上限
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_seed_then_push_only_new():
    """第一次轮询只播种，不推送历史作品；之后只推比高水位更新的。"""
    store.subscribe(group_id=100, author_id=200, baseline=0)
    client = FakeClient(
        {200: [FakeIllust(500), FakeIllust(400), FakeIllust(300)]},
        {500: FakeIllust(500, title="新作500")},
    )
    sent = []

    async def send(gid, msg):
        sent.append((gid, str(msg)))

    # 第一次：只播种
    n = await poller.poll_once(client, _cfg(), send=send)
    assert n == 0
    assert sent == []
    assert client.download_calls == []  # 播种轮不该下载任何页图
    assert store.list_by_group(100)[0]["last_seen"] == 500

    # 出现更新的作品 → 推送
    client.illusts_by_author[200] = [FakeIllust(600, title="新作600"), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, title="新作600")
    n = await poller.poll_once(client, _cfg(), send=send)
    assert n == 1
    assert "新作600" in sent[0][1]
    assert store.list_by_group(100)[0]["last_seen"] == 600


@pytest.mark.asyncio
async def test_max_push_per_poll_caps_and_keeps_id_ascending():
    """一次出现多篇新作时，按 ID 升序（从旧到新）推，且不超过上限。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(900), FakeIllust(800), FakeIllust(700), FakeIllust(500)]
    for i in (700, 800, 900):
        client.detail_map[i] = FakeIllust(i, title=f"新作{i}")
    sent = []

    async def send(gid, msg):
        sent.append(str(msg))

    n = await poller.poll_once(client, _cfg(pixiv_illust_max_push_per_poll=2), send=send)
    assert n == 2
    assert "新作700" in sent[0]  # 先推旧的
    assert "新作800" in sent[1]
    assert store.list_by_group(100)[0]["last_seen"] == 800  # 只推进到已推的最大值；900 留给下一轮

    # 下一轮把剩下的 900 补上（900 的高水位没被动过）
    client.detail_map[900] = FakeIllust(900, title="新作900")
    n = await poller.poll_once(client, _cfg(pixiv_illust_max_push_per_poll=2), send=send)
    assert n == 1
    assert "新作900" in sent[2]
    assert store.list_by_group(100)[0]["last_seen"] == 900


# ══════════════════════════════════════════════════════════════════
# ③ _is_pushable：显式 is False / is not True；缺字段照推
# ══════════════════════════════════════════════════════════════════


def test_missing_visible_field_is_treated_as_pushable():
    """JsonDict 缺字段返 None：不能因为「没这个字段」就把作品静默丢掉。

    `bool(getattr(i, "visible", True))` 这种写法在这里会返回 False
    （默认值救不了），导致作品被永久丢弃且**完全不报错**。
    """
    illust = wrap_json({"id": 999, "title": "没 visible 字段"})  # 故意不带 visible
    assert illust.visible is None  # 确认前提成立
    assert poller._is_pushable(illust) is True  # 但必须照推

    hidden = wrap_json({"id": 1, "visible": False})
    assert poller._is_pushable(hidden) is False

    mypixiv = wrap_json({"id": 2, "is_mypixiv_only": True})
    assert poller._is_pushable(mypixiv) is False


@pytest.mark.asyncio
async def test_invisible_and_mypixiv_only_are_skipped_but_watermark_moves():
    """不可见 / 仅 mypixiv 的新作不推，但高水位要跨过去（否则永远重新扫到）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [
        FakeIllust(600, is_mypixiv_only=True),
        FakeIllust(700, visible=False),
        FakeIllust(500),
    ]
    n = await poller.poll_once(client, _cfg(), send=_noop_send)
    assert n == 0
    assert store.list_by_group(100)[0]["last_seen"] == 700  # 跳过但高水位仍前进
    assert client.download_calls == []  # 被跳过的连详情/页图都没碰


# ══════════════════════════════════════════════════════════════════
# ④ R18 推送轴（关闭 → 跳过但高水位跨过去）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_r18_push_switch_off_skips_r18_without_downloads_but_advances_watermark():
    """`PIXIV_ILLUST_R18_PUSH_ENABLED=false` 时 R18 新作**完全不推**（连页图都不下）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    # 只有一篇新作，且是 R18 多页漫画
    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = _multi_page(600, 3, x_restrict=1)

    sent = []

    async def send(group_id, message):
        sent.append(message)

    n = await poller.poll_once(client, _cfg(pixiv_illust_r18_push_enabled=False), send=send)
    assert n == 0
    assert sent == []
    assert client.download_calls == []  # 被跳过的 R18 连页图都没去下
    assert store.list_by_group(100)[0]["last_seen"] == 600  # 高水位推到了 600


@pytest.mark.asyncio
async def test_r18_push_switch_off_still_pushes_non_r18():
    """关掉开关只影响 R18，同批次的全年龄新作照推（且页图不模糊）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(700), FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, x_restrict=1)  # R18
    client.detail_map[700] = FakeIllust(700, x_restrict=0)  # 全年龄

    sent = []

    async def send(group_id, message):
        sent.append(message)

    n = await poller.poll_once(client, _cfg(pixiv_illust_r18_push_enabled=False), send=send)
    assert n == 1  # 只推了全年龄那篇
    assert len(sent) == 1
    assert len(client.download_calls) == 1  # 只有全年龄那篇下了页图
    assert client.download_calls[0][1] is False  # 非 R18 不模糊


@pytest.mark.asyncio
async def test_r18_pages_are_blurred_when_enabled():
    """R18 推送开关打开（默认）时 R18 新作照推，页图按配置模糊。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, x_restrict=1)

    n = await poller.poll_once(client, _cfg(pixiv_illust_r18_push_enabled=True), send=_noop_send)
    assert n == 1
    url, blur, radius = client.download_calls[-1]
    assert url.startswith("https://i.pximg.net/")
    assert blur is True  # R18 + 模糊开关 → 要模糊
    assert radius == 9  # 模糊半径来自配置（固定 px）


@pytest.mark.asyncio
async def test_r18_blur_can_be_disabled_by_config():
    """「R18 推送轴」与「R18 页图模糊开关」是**两根独立的轴**：关模糊不影响照推。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500, x_restrict=1)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, x_restrict=1)
    n = await poller.poll_once(client, _cfg(pixiv_illust_blur_r18=False), send=_noop_send)

    assert n == 1
    assert client.download_calls[-1][1] is False


@pytest.mark.asyncio
async def test_non_r18_pages_never_blurred():
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, x_restrict=0)
    await poller.poll_once(client, _cfg(), send=_noop_send)
    assert client.download_calls[-1][1] is False


# ══════════════════════════════════════════════════════════════════
# ⑤ 群白 / 黑名单：不推，且高水位一次推到最新
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_blacklisted_group_gets_nothing_and_watermark_jumps_to_newest():
    store.subscribe(100, 200, baseline=0)
    client = FakeClient(
        {200: [FakeIllust(500), FakeIllust(600)]},
        {600: FakeIllust(600)},
    )
    sent = []

    async def send(gid, msg):
        sent.append(msg)

    n = await poller.poll_once(client, _cfg(pixiv_illust_group_blacklist=[100]), send=send)
    assert n == 0
    assert sent == []
    assert client.download_calls == []
    assert store.list_by_group(100)[0]["last_seen"] == 600  # 一次性推到位，不留任何积压


@pytest.mark.asyncio
async def test_group_outside_whitelist_gets_nothing_and_watermark_jumps_to_newest():
    store.subscribe(100, 200, baseline=0)
    client = FakeClient(
        {200: [FakeIllust(500), FakeIllust(600)]},
        {600: FakeIllust(600)},
    )
    sent = []

    async def send(gid, msg):
        sent.append(msg)

    # 白名单只放 999 → 群 100 被排除
    n = await poller.poll_once(client, _cfg(pixiv_illust_group_whitelist=[999]), send=send)
    assert n == 0
    assert sent == []
    assert store.list_by_group(100)[0]["last_seen"] == 600


@pytest.mark.asyncio
async def test_group_in_whitelist_is_served_normally():
    """反向对照：白名单内的群行为与默认完全一致（闸门没有过度拦截）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500), FakeIllust(600)]})
    sent = []

    async def send(gid, msg):
        sent.append(msg)

    # 首轮：白名单内的群也照常播种、不推历史作品
    n = await poller.poll_once(client, _cfg(pixiv_illust_group_whitelist=[100]), send=send)
    assert n == 0
    assert sent == []
    assert store.list_by_group(100)[0]["last_seen"] == 600

    # 之后有新作 → 照推
    client.illusts_by_author[200] = [FakeIllust(700), FakeIllust(600), FakeIllust(500)]
    client.detail_map[700] = FakeIllust(700, title="白名单内的新作")
    n = await poller.poll_once(client, _cfg(pixiv_illust_group_whitelist=[100]), send=send)
    assert n == 1
    assert "白名单内的新作" in sent[0].extract_plain_text()


# ══════════════════════════════════════════════════════════════════
# ⑦ 单作者异常 / 发送失败
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_author_failure_does_not_break_other_authors():
    store.subscribe(100, 200, baseline=0)
    store.subscribe(100, 300, baseline=0)

    class FlakyClient(FakeClient):
        async def user_illusts(self, author_id):
            if author_id == 200:
                raise RuntimeError("boom")
            return await super().user_illusts(author_id)

    client = FlakyClient({300: [FakeIllust(800)]}, {800: FakeIllust(800, title="别人的新作")})
    n = await poller.poll_once(client, _cfg(), send=_noop_send)
    assert n == 0  # 300 只播种
    assert store.list_by_group(100)  # 200 的异常没让整轮崩掉

    # 下一轮 300 照常工作（异常作者不影响别人）
    client.illusts_by_author[300] = [FakeIllust(900), FakeIllust(800)]
    client.detail_map[900] = FakeIllust(900, title="新作900")
    n = await poller.poll_once(client, _cfg(), send=_noop_send)
    assert n == 1
    row = next(r for r in store.list_by_group(100) if r["author_id"] == 300)
    assert row["last_seen"] == 900


@pytest.mark.asyncio
async def test_detail_failure_skips_that_work_but_keeps_the_round_and_watermark():
    """单篇详情拉取失败：不推这一篇，但高水位跨过去（避免永远卡住后面的新作）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(700), FakeIllust(600), FakeIllust(500)]

    class NoDetailClient(FakeClient):
        async def illust_detail(self, illust_id):
            if illust_id == 600:
                raise RuntimeError("detail boom")
            return await super().illust_detail(illust_id)

    client = NoDetailClient(client.illusts_by_author, {700: FakeIllust(700, title="新作700")})
    n = await poller.poll_once(client, _cfg(), send=_noop_send)
    assert n == 1  # 700 照推
    assert store.list_by_group(100)[0]["last_seen"] == 700


@pytest.mark.asyncio
async def test_send_failure_still_records_landed_and_is_not_retried():
    """发送失败也记 landed：否则下一轮反复重推同一条 = 群里的刷屏。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600)

    calls = []

    async def failing_send(gid, msg):
        calls.append(gid)
        raise RuntimeError("send boom")

    n = await poller.poll_once(client, _cfg(), send=failing_send)
    assert n == 0  # 推送失败不计入 pushed
    assert calls == [100]
    assert store.list_by_group(100)[0]["last_seen"] == 600  # 但高水位照常推进

    # 下一轮不会再重推 600
    n = await poller.poll_once(client, _cfg(), send=_noop_send)
    assert n == 0
    assert store.list_by_group(100)[0]["last_seen"] == 600


# ══════════════════════════════════════════════════════════════════
# 多页：下载前 N 页（strip_scale 取原图）、附加页只走 send_pages
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_multi_page_downloads_all_pages_and_send_pages_gets_only_the_rest():
    """多页作品：每页都下（URL 是 strip 过的原图），第 2..N 页交 send_pages。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = _multi_page(600, 3, title="多页新作")

    sent = []
    extra = []

    async def send(gid, msg):
        sent.append(msg)

    async def send_pages(gid, pages):
        extra.append((gid, list(pages)))

    n = await poller.poll_once(client, _cfg(), send=send, send_pages=send_pages)
    assert n == 1
    assert len(client.download_calls) == 3  # 3 页全下了（上限 4）
    # 接线断言：**每一页**都走过 strip_scale，没有带缩放段的缩略图
    assert all("/c/" not in url for url, _, _ in client.download_calls)
    assert [url for url, _, _ in client.download_calls] == [_original(0), _original(1), _original(2)]

    assert len(sent) == 1
    assert _image_count(sent[0]) == 1  # 第 1 条 = 文案 + 第 1 页
    assert "多页新作" in sent[0].extract_plain_text()
    assert "共 3 页" in sent[0].extract_plain_text()
    # 附加页只收到第 2..N 页（第 1 页已经在上面那条里），顺序不变
    assert extra == [(100, [b"page-2", b"page-3"])]


@pytest.mark.asyncio
async def test_single_page_uses_image_urls_large_and_no_extra_pages():
    """单页作品 `meta_pages=[]`：退化到 image_urls.large，且不调 send_pages。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600, title="单页新作")

    extra = []

    async def send_pages(gid, pages):
        extra.append((gid, list(pages)))

    sent = []

    async def send(gid, msg):
        sent.append(msg)

    n = await poller.poll_once(client, _cfg(), send=send, send_pages=send_pages)
    assert n == 1
    assert [url for url, _, _ in client.download_calls] == [_original(0)]
    assert _image_count(sent[0]) == 1
    assert extra == []  # 单页没有附加页


@pytest.mark.asyncio
async def test_max_pages_truncates_downloads_and_extra_pages():
    """`PIXIV_ILLUST_MAX_PAGES` 截断：只下前 N 页，附加页也只有剩下的那些。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = _multi_page(600, 5, title="五页新作")

    extra = []

    async def send_pages(gid, pages):
        extra.append((gid, list(pages)))

    n = await poller.poll_once(client, _cfg(pixiv_illust_max_pages=2), send=_noop_send, send_pages=send_pages)
    assert n == 1
    assert len(client.download_calls) == 2  # 只下前 2 页
    assert extra == [(100, [b"page-2"])]  # 附加页 = 第 2 页（1 页），不会多


@pytest.mark.asyncio
async def test_page_max_width_comes_from_config():
    """配置的页图宽度上限要**真的传到下载层**（默认 0 = 原图）。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = FakeIllust(600)
    await poller.poll_once(client, _cfg(pixiv_illust_image_max_width=800), send=_noop_send)
    assert client.max_widths[-1] == 800

    client.illusts_by_author[200] = [FakeIllust(700), FakeIllust(600)]
    client.detail_map[700] = FakeIllust(700)
    await poller.poll_once(client, _cfg(), send=_noop_send)
    assert client.max_widths[-1] == 0  # 默认 0 = 发原图


@pytest.mark.asyncio
async def test_send_pages_absent_or_failing_never_breaks_the_round():
    """send_pages 没注入 / 注入的回调抛异常：都不许影响主消息与高水位。"""
    store.subscribe(100, 200, baseline=0)
    client = FakeClient({200: [FakeIllust(500)]}, {500: FakeIllust(500)})
    await poller.poll_once(client, _cfg(), send=_noop_send)  # 播种

    client.illusts_by_author[200] = [FakeIllust(600), FakeIllust(500)]
    client.detail_map[600] = _multi_page(600, 3)

    sent = []

    async def send(gid, msg):
        sent.append(msg)

    # ① 不注入 send_pages：只发第 1 页，不炸
    n = await poller.poll_once(client, _cfg(), send=send)
    assert n == 1
    assert len(sent) == 1

    # ② 注入一个必然失败的回调：主消息照样送出去，高水位照常推进
    client.illusts_by_author[200] = [FakeIllust(700), FakeIllust(600)]
    client.detail_map[700] = _multi_page(700, 3)

    async def broken_send_pages(gid, pages):
        raise RuntimeError("extra boom")

    n = await poller.poll_once(client, _cfg(), send=send, send_pages=broken_send_pages)
    assert n == 1
    assert store.list_by_group(100)[0]["last_seen"] == 700


# ══════════════════════════════════════════════════════════════════
# 全链路：真 PixivClient + 真形状响应 + 真 poller
#
# 上面那些 FakeClient 的 `illust_detail` 返回的是「已解包、已就绪」的对象，
# 绕过了客户端的解包与 page_urls 两环。只有让**原始 App 响应**真正流过客户端，
# 才测得到「顶层 {"illust": {...}} 解包 → meta_pages 取页 → strip_scale 取原图」。
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_chain_real_pixiv_client_multipage_original_pages(monkeypatch):
    import nonebot_plugin_pixiv_illust.pixiv_client as pc_mod
    from nonebot_plugin_pixiv_illust.pixiv_client import PixivClient

    def _raw_illust(illust_id: int, title: str, *, x_restrict: int = 0, pages: int = 1) -> dict:
        """未解包的 App API 作品形状：**顶层 {"illust": {...}} 由客户端负责拆**。"""
        return {
            "id": illust_id,
            "title": title,
            "x_restrict": x_restrict,
            "page_count": pages,
            "image_urls": {"large": _scaled(0)},
            "meta_pages": [{"image_urls": {"large": _scaled(p)}} for p in range(pages)] if pages > 1 else [],
            "user": {"id": 200, "name": "画师甲"},
        }

    class FakeAPI:
        """冒充 pixivpy3：原样返回录下来的响应形状（**不做任何解包**）。"""

        def __init__(self, **kwargs):
            pass

        def auth(self, **kwargs):
            pass

        def user_illusts(self, author_id, type="illust"):
            # 旧作 500 + 两页 R18 新作 600；两种 type 都返回同一批（去重顺带被覆盖）
            return wrap_json(
                {"illusts": [_raw_illust(600, "两页新作", x_restrict=1, pages=2), _raw_illust(500, "旧作")]}
            )

        def illust_detail(self, illust_id):
            return wrap_json({"illust": _raw_illust(600, "两页新作", x_restrict=1, pages=2)})

    monkeypatch.setattr(pc_mod, "AppPixivAPI", FakeAPI)

    fetched = []

    async def _fake_fetch(self, url):
        fetched.append(url)
        # ⚠️ 必须真能解码：客户端会对它跑 process_image（R18 模糊）——
        # 假字节会让 PIL 抛异常、页图被静默丢掉，断言失败还掩盖真实链路。
        return _real_png_bytes()

    # ⚠️ 打桩 fetch_image：否则真客户端会真的去下 i.pximg.net（慢 + 不稳 + 要外网）
    monkeypatch.setattr(PixivClient, "fetch_image", _fake_fetch)

    store.subscribe(100, 200, baseline=0)
    store.set_last_seen(100, 200, 500)  # 旧作已看过，只留 600 算新

    sent = []
    extra = []

    async def send(gid, msg):
        sent.append(msg)

    async def send_pages(gid, pages):
        extra.append((gid, list(pages)))

    real = PixivClient("tok", "")
    n = await poller.poll_once(real, _cfg(pixiv_illust_r18_push_enabled=True), send=send, send_pages=send_pages)

    assert n == 1
    assert len(fetched) == 2  # 两页都真下了
    assert all("/c/" not in u for u in fetched)  # 且都是原图（去掉 CDN 缩放段）
    assert fetched[0].endswith("_p0_master1200.jpg")
    assert fetched[1].endswith("_p1_master1200.jpg")
    assert len(extra) == 1
    assert len(extra[0][1]) == 1  # 附加页 = 第 2 页

    text = sent[0].extract_plain_text()
    assert "两页新作" in text  # 标题（不解包会是 None）
    assert "画师甲" in text  # 画师名
    assert "R18（已模糊）" in text  # x_restrict 解包后才是 1
    assert "共 2 页" in text
    assert "https://www.pixiv.net/artworks/600" in text
    assert store.list_by_group(100)[0]["last_seen"] == 600
