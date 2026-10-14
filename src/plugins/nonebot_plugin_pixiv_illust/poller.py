"""轮询订阅画师的新作并推送到群。

核心是 last_seen 高水位（与参照仓 pixiv 小说插件**逐条同义，别改**）：
  - last_seen == 0  → 首次轮询，只播种（把高水位设成当前最新 ID），不推送
  - last_seen >  0  → 推 ID 比高水位大的作品，按 ID 升序（从旧到新），受 max_push 限制

与小说插件的差异只有三处（都是形态差，不是语义差）：
  - 列表接口是 `client.user_illusts`（illust + manga 合并），详情是 `client.illust_detail`；
  - 一篇作品可能是**多页**：取前 `pixiv_illust_max_pages` 页图（页内 URL 先 strip_scale 去
    CDN 缩放段取原图），第 1 页随文案发，第 2..N 页交给 `send_pages` 回调分流
    （≤3 直发多图 / >3 合并转发 / 库缺失降级，都由调用方决定）；
  - R18 轴读的是页图（不只是封面），但「推不推 / 糊不糊」两根轴与小说插件同款。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from nonebot.adapters.onebot.v11 import Message

from . import pixiv_client, policy, store
from .config import Config
from .message import build_push
from .pixiv_client import PixivClient

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

SendText = Callable[[int, Message], Awaitable[None]]
SendPages = Callable[[int, list[bytes]], Awaitable[None]]


def _is_pushable(illust) -> bool:
    """过滤不可见 / 仅 mypixiv 可见的作品。

    ⚠️ 这里**必须**显式判 `is False`，不能写 `bool(getattr(illust, "visible", True))`：
    pixivpy3 用 JsonDict 表示响应，对**缺失**的键返回 `None`（既不抛异常，
    也**拿不到 getattr 的默认值**）。于是 `bool(None)` = False，
    「字段缺失」会被误判成「不可见」→ 作品被**静默永久丢弃**（下次轮询高水位
    已跨过去，再也不会推）。

    缺字段时的保守方向是**照推**（宁可多推一条，也别悄悄吞掉）。
    """
    if getattr(illust, "visible", None) is False:
        return False
    return getattr(illust, "is_mypixiv_only", None) is not True


async def poll_once(
    client: PixivClient,
    config: Config,
    *,
    send: SendText,
    send_pages: SendPages | None = None,
) -> int:
    """跑一轮。返回本轮推送条数。

    `send_pages(group_id, pages)` 由调用方注入（见 __init__.py 的 `_send_extra`），
    收到的是**第 2..N 页**（第 1 页已在 `send` 的文案消息里）；为 None 时只发第 1 页。
    """
    pushed = 0

    for author_id in store.all_authors():
        try:
            illusts = await client.user_illusts(author_id)
        except Exception as e:
            # 单个画师失败不能拖垮整轮
            logger.warning(f"拉取画师 {author_id} 作品列表失败: {type(e).__name__}: {e}")
            continue

        if not illusts:
            continue

        newest_id = max(int(i.id) for i in illusts)

        for sub in store.list_by_author(author_id):
            group_id = sub["group_id"]
            last_seen = sub["last_seen"]

            # ── 群白名单 / 黑名单 ───────────────────────────────────────
            # 被排除的群（不在白名单，或已被拉黑）**完全不服务**：不推送，
            # 并把高水位一次推进到最新，不留下任何积压。
            #
            # 为什么是「推进」而不是「什么都不做」：若直接 continue，
            # 用户把这个群重新放行时，会把离线期间积压的旧作一次性推出来 ——
            # 与本插件「订阅前的历史作品不推送」的取向相反（也是刷屏）。
            # 这里一次性写到位而不是逐篇写，既省 DB 往返，也避免在
            # `landed` 永远为空的情况下把整份 fresh 列表逐条走一遍。
            if not policy.is_group_allowed(
                group_id,
                whitelist=config.pixiv_illust_group_whitelist,
                blacklist=config.pixiv_illust_group_blacklist,
            ):
                store.set_last_seen(group_id, author_id, newest_id)
                continue

            # 首次：只播种高水位，不推送历史作品
            if last_seen == 0:
                store.set_last_seen(group_id, author_id, newest_id)
                continue

            fresh = sorted(
                (i for i in illusts if int(i.id) > last_seen),
                key=lambda i: int(i.id),
            )
            if not fresh:
                continue

            # 高水位推进策略（**别改**）：
            #   按 ID 升序逐篇处理；
            #   - 不可见 / 仅 mypixiv / 被 R18 开关挡掉的 → **不推，但高水位要跨过去**，
            #     否则它们会被永远重新扫到，还会挡住后面的新作；
            #   - 推出去的 → 记进 landed；
            #   - landed 攒够 max_push 就 break，剩下的**不动高水位**，留给下一轮。
            # 循环结束后高水位 = max(landed)，即「最后一条真正推出去的」。
            landed: list[int] = []
            for illust in fresh:
                illust_id = int(illust.id)

                if not _is_pushable(illust):
                    store.set_last_seen(group_id, author_id, illust_id)
                    continue

                if len(landed) >= config.pixiv_illust_max_push_per_poll:
                    break

                try:
                    detail = await client.illust_detail(illust_id)
                except Exception as e:
                    logger.warning(f"取插画 {illust_id} 详情失败: {type(e).__name__}: {e}")
                    store.set_last_seen(group_id, author_id, illust_id)
                    continue

                x_restrict = int(getattr(detail, "x_restrict", 0) or 0)

                # R18 推送轴（独立于「R18 页图模糊」那根轴）：
                # 关闭时整条不推，也不浪费页图下载
                if x_restrict > 0 and not config.pixiv_illust_r18_push_enabled:
                    store.set_last_seen(group_id, author_id, illust_id)
                    continue

                # ── 下载前 N 页（多页插画 / 漫画，一页一张）─────────────
                # 取**原图**：App 的 meta_pages[*].image_urls.large 是 CDN 缩略
                # （带 /c/540x540_70/ 段），strip_scale 去掉缩放段才是 master1200 档。
                # 单页作品 meta_pages 为空，page_urls 会退化到 image_urls.large。
                pages: list[bytes] = []
                blurred = False
                should_blur = bool(config.pixiv_illust_blur_r18 and x_restrict > 0)
                for raw_url in pixiv_client.page_urls(detail)[: config.pixiv_illust_max_pages]:
                    url = pixiv_client.strip_scale(raw_url)
                    try:
                        page = await client.download_image(
                            url,
                            blur=should_blur,
                            radius=config.pixiv_illust_blur_radius,
                            max_width=config.pixiv_illust_image_max_width,
                        )
                    except Exception as e:
                        logger.warning(f"下载页图 {url} 失败: {type(e).__name__}: {e}")
                        continue
                    if page:
                        pages.append(page)
                # 一页都没下来（占位图/全失败）就别声称「已模糊」—— 文案会自相矛盾
                blurred = should_blur and bool(pages)

                msg = build_push(detail, pages, blurred=blurred)
                try:
                    await send(group_id, msg)
                    pushed += 1
                except Exception as e:
                    logger.warning(f"推送到群 {group_id} 失败: {type(e).__name__}: {e}")

                # 第 2..N 页交给调用方的分流回调；这一条失败只告警，
                # 不影响本条已经产生的文案消息，也不影响高水位记账
                rest = pages[1:]
                if rest and send_pages is not None:
                    try:
                        await send_pages(group_id, rest)
                    except Exception as e:
                        logger.warning(f"推送附加页到群 {group_id} 失败: {type(e).__name__}: {e}")

                # 即使发送失败也记进 landed —— 避免下一轮反复重推同一条刷屏
                landed.append(illust_id)

            if landed:
                store.set_last_seen(group_id, author_id, max(landed))

    return pushed
