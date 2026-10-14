"""作者头像：下载（带 Referer）→ 磁盘缓存 → base64 data URI。

为什么要转 data URI 而不是直接用 URL：htmlkit 的网络取图**不带 Referer**，
而 i.pximg.net 不带 Referer 就 403（已实测）。转成 data URI 后 htmlkit
用 native_data_scheme 原生解码，完全不碰网络。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

Fetcher = Callable[[str], Awaitable[bytes]]

# 同时最多下载几个头像（避免几十个并发把 pixiv 惹毛）
_CONCURRENCY = 4

_cache_dir: Path | None = None


def init(path: Path) -> None:
    """指定缓存目录（生产用 localstore 的缓存目录，测试传 tmp_path）。"""
    global _cache_dir
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _cache_dir = path


def _ensure_cache_dir() -> Path:
    if _cache_dir is None:
        raise RuntimeError("avatars.init() 还没调用")
    return _cache_dir


def _cache_file(url: str) -> Path:
    return _ensure_cache_dir() / f"{hashlib.sha256(url.encode()).hexdigest()}.bin"


def _sniff_mime(data: bytes) -> str:
    """按**magic bytes**判断图片类型。

    ⚠️ 不能像初稿那样硬编码 `image/jpeg` —— pixiv 头像确实有 PNG
    （实测：URL 结尾 `.png`、内容开头 `\\x89PNG\\r\\n\\x1a\\n`，却被标成 jpeg）。
    MIME 与实际内容不符时，浏览器/渲染器可能拒绝显示，头像就白了。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # 兜底：htmlkit 那边仍会按内容解码


def _to_data_uri(data: bytes) -> str:
    """包成 data URI（MIME 按真实内容嗅探）。htmlkit 的 native_data_scheme 会原生解码。"""
    mime = _sniff_mime(data)
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


async def data_uri(url: str, *, fetch: Fetcher) -> str | None:
    """取头像的 data URI。**任何失败都返回 None**（模板会退化成占位块）。

    失败**不写负缓存** —— 否则一次网络抖动会让头像永久缺失。
    """
    if not url:
        return None

    path = _cache_file(url)
    if path.is_file():
        try:
            return _to_data_uri(path.read_bytes())
        except OSError as e:  # 缓存文件坏了就当没有
            logger.warning(f"读取头像缓存失败，重新下载: {e}")

    try:
        data = await fetch(url)
    except Exception as e:
        logger.warning(f"下载头像失败 {url}: {type(e).__name__}: {e}")
        return None

    try:
        path.write_bytes(data)
    except OSError as e:
        logger.warning(f"写头像缓存失败（不影响本次显示）: {e}")

    return _to_data_uri(data)


async def for_rows(rows: Iterable[Mapping[str, Any]], *, fetch: Fetcher) -> dict[str, str]:
    """给一批订阅行批量取头像，返回 `{头像URL: dataURI}`（失败的不在结果里）。

    同一个 URL 只下载一次（作者被多个群订阅时会有重复）。
    """
    urls = sorted({str(r["author_avatar_url"]) for r in rows if r["author_avatar_url"]})
    if not urls:
        return {}

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def one(url: str) -> tuple[str, str | None]:
        async with semaphore:
            return url, await data_uri(url, fetch=fetch)

    results = await asyncio.gather(*(one(u) for u in urls))
    return {url: uri for url, uri in results if uri}
