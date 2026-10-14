"""pixivpy3 的异步包装（插画/漫画）。

pixivpy3 是同步库（基于 requests），在异步机器人里直接调用会**阻塞整个事件循环**
（表现为机器人卡住不响应）。所以每个 API 调用都用 asyncio.to_thread() 丢进线程池。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from PIL import Image, ImageFilter
from pixivpy3 import AppPixivAPI, PixivError

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

# i.pximg.net 会校验 Referer，不带就 403（已实测）
REFERER = "https://www.pixiv.net/"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# ── 鉴权有效期：必须**主动换**，不能等失败 ──────────────────────────
#
# ⚠️⚠️ 实测（2026-10-04，小说插件生产故障复盘；本插件走**同一套 App API 与同一个
#   pixivpy3**，所以这层防护必须原样继承）：
#   pixiv 的 access_token 只有 **1 小时**寿命（`/auth/token` 返回 `expires_in: 3600`），
#   而 pixivpy3 的 `require_auth()` **只判 `access_token is None`、不判过期**，
#   全库也**没有任何 401 自动重试**。
#
#   失效时的表现更坑：pixiv 返回 **HTTP 200 + 错误 JSON**
#   （`{'error': {'message': '...invalid_grant'}}`），而 pixivpy3 的 `parse_result()`
#   只做 `json.loads`、**完全不看状态码也不抛异常**。于是：
#     · `illust_detail()` 静默返回 `{'error': ...}` → `x_restrict`/`user`/`meta_pages`
#       全变 None（R18 判定失效 + URL hook 里 `user.name` 直接 AttributeError）
#     · 原来的 `_retry_sync` 只捕异常 → **既不重试、更不重鉴权**
#     · 净效果：一次鉴权过期 = **永久失败到进程重启**，生产日志里就表现为
#       「取插画 <id> 失败，确认链接是否有效」，且**进程越老越失败**
#
#   对策两层，**缺一不可**：`_get_api()` 按本 TTL 主动重建鉴权（正面解决过期），
#   `_call()` 把错误载荷当失败抛出（兜住边界与其它失效原因）。
_AUTH_TTL_SECONDS = 50 * 60  # 留 10 分钟余量，避开「刚好在边界失效」

# 成功响应绝不带顶层 `error`；这些键用于双保险（万一 pixiv 将来在成功体里也塞 error）。
_OK_PAYLOAD_KEYS = ("illust", "illusts", "next_url", "id", "title")


def build_api(refresh_token: str, proxy: str) -> AppPixivAPI:
    """构造已鉴权的 AppPixivAPI。

    ⚠️ requests 的参数名是 `proxies`（dict），不是 `proxy`；写成 proxy= 会静默失效。
    """
    kwargs: dict[str, Any] = {}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    api = AppPixivAPI(**kwargs)
    api.auth(refresh_token=refresh_token)
    return api


def blur_image(data: bytes, radius: int) -> bytes:
    """高斯模糊。**半径是固定像素值**（配置项 PIXIV_ILLUST_BLUR_RADIUS，6–12px）。

    不按图片尺寸缩放 —— 用户明确要求固定 px 半径。
    统一转 RGB 并以 JPEG 输出（插画是照片类内容，JPEG 体积小）。
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img = img.filter(ImageFilter.GaussianBlur(radius=radius))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ── 页图原图 / 占位图 / 尺寸 ────────────────────────────────────────
#
# CDN 的缩放段：`/c/540x540_70/`、`/c/240x480_80/` 这种。**去掉它就是原图**。
# 插画/漫画的 App `image_urls.large`（含 `meta_pages[*]`）实测就是
# `…/c/540x540_70/img-master/…_p0_master1200.jpg`；去掉 /c/ 段拿到的是同一个
# 文件的 1200px 档（小说插件在**同一个 CDN** 上量过像素）——
# 所以「App 给的是小图」不是接口差，而是缩放档位；**不需要换接口**。
_SCALE_RE = re.compile(r"/c/\d+x\d+(?:_\d+)?/")

# pixiv 用这些图代替「不可见 / 受限 / 不存在」的页图。
# ⚠️ App 接口对这类作品**不抛异常**，而是返回占位图 URL（实测作品 29000000），
# 不检查就会把 100x100 的 limit_unknown 图当页图发进群里。
_PLACEHOLDER_URL_MARKERS = ("limit_unknown", "/common/images/", "no_profile")

# 真实插画页图最小也是 540x540（缩略档）；占位图是 100x100。160 是两者之间的安全阈值。
_PLACEHOLDER_MAX_SIDE = 160


def strip_scale(url: str) -> str:
    """把 CDN 缩放段去掉 → **原图**（`…_p0_master1200.jpg`，1200px 档）。

    ⚠️ 只对 `_master1200` 结尾的页图合适：`_square1200` 是**裁切过的方图**，
    去掉 /c/ 段会拿到 1200x1200 方图（不是插画比例）→ 这种原样返回。
    头像 URL 没有 /c/ 段（`…/xxx_170.jpg`），套用无副作用。
    """
    if not url:
        return ""
    if "_square" in url:
        return url
    return _SCALE_RE.sub("/", url)


def is_placeholder_url(url: str) -> bool:
    """URL 层面就能判出占位图 —— 省掉一次网络请求。"""
    if not url:
        return True
    low = url.lower()
    return any(m in low for m in _PLACEHOLDER_URL_MARKERS)


def image_size(data: bytes) -> tuple[int, int] | None:
    """读图片尺寸（PIL 懒加载，只读头部，不解码像素）。读不出来返 None。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def is_placeholder_image(data: bytes) -> bool:
    """字节层面兜底：尺寸过小就当占位图（防住 URL 特征没覆盖到的占位图）。"""
    size = image_size(data)
    if size is None:
        return False  # 解不开的图交给后面的流程报错，别在这儿吞掉
    return min(size) <= _PLACEHOLDER_MAX_SIDE


def process_image(data: bytes, *, blur: bool, radius: int, max_width: int = 0) -> bytes:
    """页图后处理：先按需缩放，再按需模糊。统一以 JPEG 输出。

    ⚠️ 顺序是**先缩放、后模糊**：配置的半径是「固定像素」（用户明确要求 6–12px），
    必须作用在**最终发出的那张图**上；先模糊再缩放会把半径一起缩掉，
    「固定 9px」的语义就没了。

    `max_width<=0` 表示不缩放（发原图）。既没缩放也没模糊时**原样返回字节**，
    不重编码（省 CPU、也不掉一次质量）。
    """
    img = Image.open(io.BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")

    resized = False
    if max_width > 0 and img.width > max_width:
        height = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        resized = True

    if blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=radius))

    if not resized and not blur:
        return data

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# 解包后我们**依赖**的字段。缺任何一个都会让功能静默降级，
# 所以解包后要显式体检一遍并告警（见 `_check_illust_shape`）。
# `meta_pages` 同样要查：缺了它多页作品会**静默只发第 1 页**（退化到 image_urls.large）。
_REQUIRED_ILLUST_FIELDS = ("id", "title", "x_restrict", "image_urls", "user", "meta_pages")


def is_error_payload(resp: Any) -> bool:
    """pixivpy3 **静默返回**的错误载荷（HTTP 200 + 错误体）。

    ⚠️ 实测（2026-10-04）：bearer 过期时 pixiv 不是返回 401，而是
    **HTTP 200 + `{'error': {'message': '...invalid_grant'}}`**；pixivpy3 的
    `parse_result()` 只做 `json.loads`、不看状态码也不抛异常 —— 调用方会把它
    当数据用（`x_restrict`/`user`/`meta_pages` 全是 None）而**不报错**。

    所以必须在统一出口显式断言，否则 `_retry_sync` 永远不会触发。
    """
    if not isinstance(resp, dict) or "error" not in resp:
        return False
    return not any(k in resp for k in _OK_PAYLOAD_KEYS)


def unwrap_illust_shape(resp: Any) -> Any:
    """把 `/v1/illust/detail` 的响应解包到「作品本体」。

    ⚠️⚠️ 该接口的**顶层是 `{"illust": {...}}`，所有字段都在下一层**（App API 形状）。
    直接返回顶层对象会静默造成三处功能失效，因为 pixivpy3 的 `JsonDict`
    对**缺失的键返回 None 而不是抛异常** —— `getattr(d, "x_restrict", 0)` 这种
    带默认值的写法**拿不到默认值**，只会拿到 None：

      · `detail.x_restrict` → None → R18 判定恒为 0
        → R18 作品会被当普通作品推给群，**违反「R18 两轴」的硬要求**
        → `PIXIV_ILLUST_R18_PUSH_ENABLED` 开关也彻底失效
      · `detail.meta_pages` → None → 多页作品静默只发第 1 页
      · `detail.title` → None → 推送文案变成「🖼 None」
      · `detail.user` → None → URL hook 里 `user.name` 直接 AttributeError

    这几条都**不会报错**，只会在群里表现为「功能莫名其妙不生效」。
    """
    return resp["illust"] if isinstance(resp, dict) and "illust" in resp else resp


def _check_illust_shape(illust: Any) -> None:
    """解包后体检：字段缺失就大声告警，别让结构变动静默降级。"""
    missing = [f for f in _REQUIRED_ILLUST_FIELDS if illust.get(f) is None]
    if missing:
        logger.warning(
            f"illust_detail 解包后缺少字段 {missing} —— pixiv 响应结构可能变了。"
            f"现有键={sorted(illust.keys())[:20] if hasattr(illust, 'keys') else illust!r}. "
            f"依赖这些字段的功能（R18 判定/多页页图/文案）会降级。"
        )


def page_urls(illust: Any) -> list[str]:
    """插画/漫画的每页大图 URL（带 CDN 缩放段，交给采集端 strip_scale）。

    多页走 meta_pages；单页 meta_pages 为空，退化到 image_urls.large。
    """
    urls: list[str] = []
    for page in getattr(illust, "meta_pages", None) or []:
        u = getattr(getattr(page, "image_urls", None), "large", "") or ""
        if u:
            urls.append(u)
    if not urls:
        u = getattr(getattr(illust, "image_urls", None), "large", "") or ""
        if u:
            urls.append(u)
    return urls


def _retry_sync(fn: Callable[[], Any], tries: int = 3, delay: float = 2.0) -> Any:
    """同步重试 —— pixiv 经代理偶发 SSL EOF / 连接重置（实测 10 分钟内遇到两次）。

    没有这层重试时，一次网络抖动就让**整轮轮询**白跑：所有订阅画师都拉不到，
    这一轮的新作被跳过（下次轮询时它们已不是「比高水位更新」，会被永久漏掉）。
    """
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt + 1 < tries:
                logger.debug(f"pixiv 调用第 {attempt + 1}/{tries} 次失败，重试：{e}")
                time.sleep(delay)
    assert last is not None
    raise last


class PixivClient:
    """持有单个 AppPixivAPI 实例，惰性构造并复用。"""

    def __init__(self, refresh_token: str, proxy: str = "") -> None:
        self.refresh_token = refresh_token
        self.proxy = proxy
        self._api: AppPixivAPI | None = None
        # 上次鉴权时刻（monotonic）。pixiv 的 token 1 小时过期，必须自己盯 TTL。
        self._auth_at: float = 0.0

    def _get_api(self) -> AppPixivAPI:
        """取已鉴权客户端；**到期主动重建**（见 `_AUTH_TTL_SECONDS` 的实测说明）。

        ⚠️ 不要退化成「为空才构造」：pixivpy3 不会自己换 token，
        缓存一个实例到底就是「1 小时后永久失败」。
        """
        now = time.monotonic()
        if self._api is None or (now - self._auth_at) >= _AUTH_TTL_SECONDS:
            self._api = build_api(self.refresh_token, self.proxy)
            self._auth_at = time.monotonic()
        return self._api

    def _invalidate_api(self) -> None:
        """丢弃缓存的客户端 —— 下次 `_get_api()` 会重新 `auth()` 换新 token。"""
        self._api = None

    def _call(self, fn: Callable[[], Any]) -> Any:
        """所有 App 接口的统一出口：**错误载荷当失败** + 重试 + 自愈。

        ⚠️ 这一步是「鉴权过期后永久失败」的修复关键：pixivpy3 在 bearer 失效时
        **不抛异常**（返回 `{'error': ...}`），而 `_retry_sync` 只捕异常 ——
        不在这里断言，就既不重试也不重鉴权，故障会一直持续到进程重启。

        `fn` 内部**必须每次重新调 `self._get_api()`**（别在外面先取好实例），
        这样重试路径才拿得到重建过的新鉴权。
        """

        def _once() -> Any:
            try:
                resp = fn()
            except Exception:
                # 任何异常都可能是「这个实例已失效」（token 过期时部分接口是
                # 抛 PixivError 的）→ 丢掉它，让重试拿到新鉴权再试。
                self._invalidate_api()
                raise
            if is_error_payload(resp):
                self._invalidate_api()
                raise PixivError(f"pixiv 返回错误载荷（已丢弃鉴权缓存，下次重试重新 auth）：{resp.get('error')}")
            return resp

        return _retry_sync(_once)

    async def user_illusts(self, author_id: int) -> list[Any]:
        """画师的全部插画+漫画（App API 的 type 分两次取，按 id 去重升序）。

        ⚠️ type='illust' **不包含**漫画，必须再取一次 type='manga' 合并。
        """

        def _run() -> list[Any]:
            out: dict[int, Any] = {}
            for kind in ("illust", "manga"):
                resp = self._call(lambda k=kind: self._get_api().user_illusts(author_id, type=k))
                for it in getattr(resp, "illusts", None) or []:
                    out[int(it.id)] = it
            return [out[k] for k in sorted(out)]

        return await asyncio.to_thread(_run)

    async def author_info(self, author_id: int) -> tuple[str, str]:
        """取画师的 `(名字, 头像URL)`。取不到时返回 `("", "")`，**不抛异常**。

        订阅时顺手存下来，供订阅列表显示。

        ⚠️ 优先读列表响应**顶层**的 `user`；顶层没有时退到**首条作品的 `user`**
        （列表项一定带 `user`，这样两种响应形状都兜得住）。
        """

        def _run() -> tuple[str, str]:
            try:
                result = self._call(lambda: self._get_api().user_illusts(author_id))
                user = getattr(result, "user", None)
                if user is None:
                    items = getattr(result, "illusts", None) or []
                    user = getattr(items[0], "user", None) if items else None
                if user is None:
                    return "", ""
                name = str(getattr(user, "name", "") or "")
                avatar = str(getattr(getattr(user, "profile_image_urls", None), "medium", "") or "")
                return name, avatar
            except Exception as e:
                logger.warning(f"取画师 {author_id} 信息失败: {type(e).__name__}: {e}")
                return "", ""

        return await asyncio.to_thread(_run)

    async def illust_detail(self, illust_id: int) -> Any:
        """作品详情 → **已解包的**作品本体（`x_restrict` / `meta_pages` / `image_urls` / `title`）。

        ⚠️ 解包是必须的：接口顶层是 `{"illust": {...}}`，字段在下一层。
        不解包会让 `detail.x_restrict` 恒为 None（→ R18 判定失效）、
        `detail.meta_pages` 恒为 None（→ 多页静默只发第 1 页）、
        `detail.user` 恒为 None（→ URL hook 里 AttributeError）。
        详见 `unwrap_illust_shape`。
        """

        def _run() -> Any:
            illust = unwrap_illust_shape(self._call(lambda: self._get_api().illust_detail(illust_id)))
            _check_illust_shape(illust)
            return illust

        return await asyncio.to_thread(_run)

    async def fetch_image(self, url: str) -> bytes:
        """下载图片（页图/头像通用）。**必须带 Referer**，否则 i.pximg.net 返回 403。"""
        headers = {"Referer": REFERER, "User-Agent": _UA}
        kwargs: dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers, **kwargs) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def download_image(self, url: str, *, blur: bool, radius: int, max_width: int = 0) -> bytes:
        """下载一页插画 → 按需缩放到 `max_width` → 按需高斯模糊（`radius` 像素）。

        ⚠️ 进来先 `strip_scale()`：App 给的 `meta_pages[*].image_urls.large` 带
        `/c/540x540_70/` 缩放段，**不 strip 就永远只发缩略图**（而且不报错）。
        调用方已经 strip 过的 URL 再 strip 一次是幂等的（hook / poller 都会调）。

        页图**不可用**时返回**空字节**（而不是抛异常），调用方按「这页没图」处理：
          · URL 是 pixiv 占位图（作品不可见/受限，App 接口不抛异常只换 URL）
          · 下到的图尺寸过小（字节层面兜底）
        这两种情况打 warning，不静默。
        """
        if not url:
            return b""
        if is_placeholder_url(url):
            logger.warning(f"页图是 pixiv 占位图（作品不可见/受限），本次不带图：{url}")
            return b""
        data = await self.fetch_image(strip_scale(url))
        if is_placeholder_image(data):
            logger.warning(f"页图下到占位图 {image_size(data)}（作品不可见/受限），本次不带图：{url}")
            return b""
        return await asyncio.to_thread(process_image, data, blur=blur, radius=radius, max_width=max_width)
