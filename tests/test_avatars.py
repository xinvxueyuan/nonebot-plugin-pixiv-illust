import base64
import io

import pytest
from nonebot_plugin_pixiv_illust import avatars
from PIL import Image


def _png(size=(64, 64), color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path):
    avatars.init(tmp_path / "cache")
    yield


class FakeFetcher:
    def __init__(self, data=None, fail=False):
        self.data = data if data is not None else _png()
        self.fail = fail
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("boom")
        return self.data


@pytest.mark.asyncio
async def test_returns_data_uri():
    f = FakeFetcher()
    out = await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=f)
    assert out.startswith("data:")
    assert "base64," in out
    # 解出来的必须还是同一张图
    payload = out.split("base64,", 1)[1]
    assert Image.open(io.BytesIO(base64.b64decode(payload))).size == (64, 64)


@pytest.mark.asyncio
async def test_second_call_hits_cache_not_network():
    f = FakeFetcher()
    first = await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=f)
    second = await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=f)
    assert first == second
    assert len(f.calls) == 1  # 第二次没再下载


@pytest.mark.asyncio
async def test_different_urls_are_cached_separately():
    f = FakeFetcher()
    await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=f)
    await avatars.data_uri("https://i.pximg.net/b.jpg", fetch=f)
    assert len(f.calls) == 2


@pytest.mark.asyncio
async def test_returns_none_on_fetch_failure():
    """取不到头像不能炸，返回 None，模板会退化成占位块。"""
    f = FakeFetcher(fail=True)
    assert await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=f) is None


@pytest.mark.asyncio
async def test_returns_none_on_empty_url():
    f = FakeFetcher()
    assert await avatars.data_uri("", fetch=f) is None
    assert f.calls == []  # 空 URL 不该发请求


@pytest.mark.asyncio
async def test_failure_is_not_cached_as_negative():
    """第一次失败不该把失败状态缓存下来 —— 第二次要能成功。"""
    bad = FakeFetcher(fail=True)
    assert await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=bad) is None
    good = FakeFetcher()
    assert await avatars.data_uri("https://i.pximg.net/a.jpg", fetch=good) is not None
    assert len(good.calls) == 1


@pytest.mark.asyncio
async def test_gather_for_rows_builds_url_keyed_mapping():
    f = FakeFetcher()
    rows = [
        {"author_id": 1, "author_avatar_url": "https://i.pximg.net/a.jpg"},
        {"author_id": 2, "author_avatar_url": "https://i.pximg.net/b.jpg"},
        {"author_id": 3, "author_avatar_url": ""},  # 没头像
    ]
    mapping = await avatars.for_rows(rows, fetch=f)
    assert set(mapping) == {"https://i.pximg.net/a.jpg", "https://i.pximg.net/b.jpg"}
    assert mapping["https://i.pximg.net/a.jpg"].startswith("data:")
    assert len(f.calls) == 2  # 空 URL 不发请求


@pytest.mark.asyncio
async def test_gather_for_rows_dedupes_same_avatar():
    """同一作者被多个群订阅时会有重复 URL，只该下载一次。"""
    f = FakeFetcher()
    rows = [
        {"author_id": 1, "author_avatar_url": "https://i.pximg.net/a.jpg"},
        {"author_id": 1, "author_avatar_url": "https://i.pximg.net/a.jpg"},
    ]
    mapping = await avatars.for_rows(rows, fetch=f)
    assert len(mapping) == 1
    assert len(f.calls) == 1


@pytest.mark.asyncio
async def test_gather_for_rows_is_resilient_to_partial_failure():
    """一个头像挂掉不影响其他的。"""

    class Flaky:
        def __init__(self):
            self.calls = []

        async def __call__(self, url):
            self.calls.append(url)
            if "bad" in url:
                raise RuntimeError("boom")
            return _png()

    rows = [
        {"author_id": 1, "author_avatar_url": "https://i.pximg.net/bad.jpg"},
        {"author_id": 2, "author_avatar_url": "https://i.pximg.net/ok.jpg"},
    ]
    mapping = await avatars.for_rows(rows, fetch=Flaky())
    assert "https://i.pximg.net/bad.jpg" not in mapping  # 失败的没进来
    assert "https://i.pximg.net/ok.jpg" in mapping


# ── data URI 的 MIME 必须按真实内容嗅探 ──────────────────────
# 实测：pixiv 头像确实有 PNG（URL 结尾 .png、内容 \x89PNG...），
# 而初稿把 MIME 硬编码成 image/jpeg → 前缀与内容不符。


def test_data_uri_uses_png_mime_for_png_content():
    """真实场景：URL 是 .png、内容是 PNG → 前缀必须是 image/png。"""
    png = _png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert avatars._to_data_uri(png).startswith("data:image/png;base64,")


def test_data_uri_uses_jpeg_mime_for_jpeg_content():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 30, 30)).save(buf, format="JPEG")
    jpg = buf.getvalue()
    assert jpg.startswith(b"\xff\xd8\xff")
    assert avatars._to_data_uri(jpg).startswith("data:image/jpeg;base64,")


def test_data_uri_uses_gif_and_webp_mime():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="GIF")
    assert avatars._to_data_uri(buf.getvalue()).startswith("data:image/gif;base64,")

    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="WEBP")
    assert avatars._to_data_uri(buf.getvalue()).startswith("data:image/webp;base64,")


def test_data_uri_payload_is_intact_regardless_of_mime():
    """嗅探只影响前缀，base64 内容必须原样可解回原字节。"""
    png = _png()
    uri = avatars._to_data_uri(png)
    b64 = uri.split(",", 1)[1]
    assert base64.b64decode(b64) == png


def test_data_uri_unknown_format_falls_back_to_jpeg_label():
    """认不出的格式给个兜底 MIME，不能抛异常（渲染器仍按内容解码）。"""
    assert avatars._to_data_uri(b"\x00\x01unknownbytes").startswith("data:image/jpeg;base64,")
