"""pixiv 插画·漫画订阅推送插件。

命令（裸词触发，qbot 的 COMMAND_START=[""]）：

    订阅插画 <画师id>    群聊。本群订阅该画师的新作（插画 + 漫画）
    退订插画 <画师id>    群聊
    插画订阅列表         群聊。htmlkit 渲染成图片

参数支持直接粘贴 pixiv 链接（`artworks/123`、`member_illust.php?...illust_id=123`、`i/123`）。

三条硬约束（用户明确要求）：
  - **不引用**：回复一律用 `finish()` 直接发，**不使用引用回复**（不传 reply 消息段）。
  - **不共享**：订阅数据按 `(group_id, author_id)` 存，**各群之间不共享**。
  - **只发 release**：本项目只通过 GitHub Release 分发，不发布到 PyPI。

两条被动/后台链路：
  - **URL hook**（`on_message`，priority=20/block=False）：消息里出现 pixiv 插画/漫画链接就回
    「文案 + 前 N 页图」（R18 按配置打码；用户主动贴的链接**照发**，不受 R18 推送轴约束）。
  - **定时轮询**：按订阅画师拉新作推送。多页作品的第 2..N 页由 `_send_extra` 分流
    （≤3 直发多图 / >3 合并转发 / merge-forward 缺失时降级为多图一条，绝不让插件加载失败）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nonebot import get_bot, get_driver, get_plugin_config, on_command, on_message, require
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.log import LoguruHandler
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

# NoneBot 把 stdlib `logging` 的 root logger 钉在 WARNING，且 root **没有任何 handler**
# （实测 `root level: 30` / `root handlers: []`），所以本插件的 `logger.info(...)`
# **一条都不会出现在 journal 里** —— 只有 warning 靠 logging 的 lastResort 落到 stderr。
# 后果是启动行「已启动：代理=… 间隔=…」和推送通知全都看不见，出事时无从判断。
#
# 修法：挂上 NoneBot 自带的 `LoguruHandler`（stdlib logging → loguru 的官方桥），
# 并把本 logger 的级别降到 INFO。7 个模块用的都是**同一个** logger 名字，
# 所以这两行一次覆盖全部。
#   · 加了 handler 之后 warning 不会重复（有 handler 就不会走 lastResort）
#   · debug 仍按 INFO 级别过滤掉
if not any(isinstance(h, LoguruHandler) for h in logger.handlers):
    logger.addHandler(LoguruHandler())
logger.setLevel(logging.INFO)

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

# ⚠️ htmlkit 是**可选**依赖，必须用 try 包住。
#
# `require()` 内部会 `load_plugin()` 并把异常**原样抛出**，所以一旦 htmlkit 在某台机器上
# 加载不了（C++ 扩展缺 .so / glibc 太老 / 没装），这一行会让**整个 pixiv 插件**加载失败 ——
# 3 个命令全部消失，连「不依赖 htmlkit 的」订阅/退订都跟着没法用。
#
# 正确行为：htmlkit 不可用 → 插件照常加载 → 只有「插画订阅列表」降级成纯文本。
# render.py 里的 `_load_htmlkit()` 是惰性 import，`render_subscription_list()`
# 会捕获异常返回 None，由调用方回退 `handlers.reply_list()`。
HTMLKIT_AVAILABLE = True
try:
    require("nonebot_plugin_htmlkit")  # htmlkit 要求先 require 再 import
except Exception as e:
    HTMLKIT_AVAILABLE = False
    logger.warning(f"nonebot_plugin_htmlkit 加载失败，插画订阅列表将回退为纯文本列表：{type(e).__name__}: {e}")

from nonebot_plugin_apscheduler import scheduler

from . import avatars, handlers, message, pixiv_client, policy, render, store, urls
from .config import Config
from .message import build_extra_pages, pages_need_forward
from .pixiv_client import PixivClient
from .poller import poll_once

plugin_config = get_plugin_config(Config)

__plugin_meta__ = PluginMetadata(
    name="pixiv 插画·漫画",
    description=(
        "pixiv 插画/漫画订阅推送：按群订阅画师新作自动推送，多页作品发前 N 页（R18 图片可打码，推送/打码两轴可配）"
    ),
    usage=(
        "订阅插画 <画师id>    本群订阅该画师新作（仅群聊）\n"
        "退订插画 <画师id>    取消订阅（仅群聊）\n"
        "插画订阅列表         查看本群订阅（渲染成图片）\n"
        "参数支持直接粘贴 pixiv 链接；多页作品一条消息最多发前 N 页"
    ),
    type="application",
    homepage="https://github.com/xinvxueyuan/nonebot-plugin-pixiv-illust",
    supported_adapters={"nonebot.adapters.onebot.v11"},
    extra={
        "menu_type": "功能",
        "order": 26,
        "commands": [
            {"command": "订阅插画 <画师id>", "desc": "本群订阅该画师新作（仅群聊）"},
            {"command": "退订插画 <画师id>", "desc": "取消订阅（仅群聊）"},
            {"command": "插画订阅列表", "desc": "查看本群订阅（渲染成图片）"},
        ],
    },
)

DENIED_MSG = "此指令仅管理员可用"
GROUP_DENIED_MSG = "本插件未在此群启用（不在群白名单内，或已被列入群黑名单）"

# ── 可选依赖：合并转发（>3 页时用于第 2..N 页）────────────────────
#
# 与下面的表态库同款处理：**库缺失只降级，绝不让插件加载失败**。
# 降级路径 = 「多图一条消息」（`build_extra_pages`），功能仍可用，只是不合并转发。
_mf_build_nodes = _mf_send_forward = None
try:
    _mf = require("nonebot_plugin_merge_forward")
    _mf_build_nodes, _mf_send_forward = _mf.build_nodes, _mf.send_group_forward_msg
except Exception as e:  # 库缺失只降级，不炸插件
    logger.warning(f"merge-forward 不可用，>3 页将退化为多图一条: {type(e).__name__}: {e}")


# ── 可选依赖：表情表态（慢操作期间的 🔨/🍼/❌ 三态反馈）──────────────
#
# ⚠️ 这里**必须是可选依赖**：库没装（或它依赖的 alconna 不可用）时插件要照常工作，
# 只是没有表态。表态是锦上添花，绝不能让「库缺失」表现成「整个插件加载失败」。
reaction_available = True
try:
    from nonebot_plugin_message_reaction import message_reaction, with_reaction
except Exception as e:
    reaction_available = False
    logger.info(f"表态库 nonebot-plugin-message-reaction 不可用，本次不带表情反馈：{type(e).__name__}: {e}")

    def message_reaction(event: MessageEvent, status: str) -> bool:
        """降级替身：永远返回「没发出」，调用方无需感知库是否存在。"""
        return False

    def with_reaction(func):
        """降级替身：直接透传被装饰的函数。"""
        return func


# ── 全局客户端（refresh_token / 代理来自配置）──────────────────────
def _resolve_refresh_token() -> str:
    """pixiv refresh_token：插件自己的键优先，**为空时回退全局 `PIXIV_REFRESH_TOKEN`**。

    回退的动机：两个 pixiv 插件（小说 / 插画）用的是**同一个 pixiv 账号**、共用一份凭据，
    换 token 时只想改一处；否则很容易出现「改了全局、插画这边还是旧 token」的静默失效
    （表现为轮询一直不推，日志里只有一句不容易注意到的报错）。

    回退时打 `debug` 而不是 `info`：这个函数每轮轮询都会调一次，info 会刷屏。
    """
    own = str(plugin_config.pixiv_illust_refresh_token or "").strip()
    if own:
        return own
    fallback = str(getattr(get_driver().config, "pixiv_refresh_token", "") or "").strip()
    if fallback:
        logger.debug("PIXIV_ILLUST_REFRESH_TOKEN 为空，回退全局 PIXIV_REFRESH_TOKEN")
    return fallback


client = PixivClient(_resolve_refresh_token(), plugin_config.pixiv_illust_proxy)

driver = get_driver()


def _is_admin(event: MessageEvent) -> bool:
    """管理员闸门：这个用户**能不能用**命令。逻辑在 policy.is_admin（纯函数，已单测）。

    这里**不**用 `permission=` 参数，而是放在 handler 内部：
    因为 NoneBot 的 permission 失败是**静默忽略**，用户会以为机器人坏了。
    放内部就能明确回一句「此指令仅管理员可用」。
    """
    return policy.is_admin(
        event.get_user_id(),
        admin_only=plugin_config.pixiv_illust_admin_only,
        admin_ids=plugin_config.pixiv_illust_admin_ids,
        superusers=list(get_driver().config.superusers),
    )


def _is_group_allowed(event: MessageEvent) -> bool:
    """群白名单/黑名单闸门：这个群能不能用插件。逻辑在 policy.is_group_allowed（纯函数，已单测）。

    两份名单都为空 = 功能关闭、全放行（默认）。黑名单优先于白名单。
    私聊不算「群」，不受这两份名单约束 —— 否的话用户配了名单就再也没法私聊贴链接了，
    而它们管的是「限制群」。
    """
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    return policy.is_group_allowed(
        group_id,
        whitelist=plugin_config.pixiv_illust_group_whitelist,
        blacklist=plugin_config.pixiv_illust_group_blacklist,
    )


async def _send(group_id: int, message: Message) -> None:
    """主动推送到群（走 OneBot V11 的 send_group_msg）。"""
    await get_bot().send_group_msg(group_id=group_id, message=message)


async def _send_extra(group_id: int, rest: list[bytes]) -> None:
    """把「第 2..N 页」发出去：>3 页走合并转发，否则一条多图消息。

    分流阈值由 `message.pages_need_forward` 决定（≤3 直发 / >3 转发，用户 2026-10-14 拍板）。

    两条降级路径，都**不许**把整条推送弄丢：
      · merge-forward 库缺失（`_mf_*` 为 None）→ 多图一条；
      · 转发调用本身失败（协议端不支持 / 网络抖动）→ 同上，并打 warning。
    """
    bot = get_bot()
    if pages_need_forward(len(rest)) and _mf_build_nodes is not None and _mf_send_forward is not None:
        try:
            nodes = _mf_build_nodes([Message(MessageSegment.image(p)) for p in rest], bot.self_id, "pixiv 插画")
            await _mf_send_forward(bot, group_id, nodes)
            return
        except Exception as e:
            logger.warning(f"合并转发失败，退化为多图一条: {type(e).__name__}: {e}")

    msg = build_extra_pages(rest)  # ≤3 或库缺失/转发失败：多图一条
    if msg is not None:
        await bot.send_group_msg(group_id=group_id, message=msg)


async def _fetch_pages(detail: Any, *, blur: bool) -> list[bytes]:
    """下载作品的前 `pixiv_illust_max_pages` 页（每页先 `strip_scale` 去 CDN 缩放段取原图）。

    页级失败**只告警跳过**：多页作品少一页也好过整条推送挂掉。
    """
    pages: list[bytes] = []
    for raw_url in pixiv_client.page_urls(detail)[: plugin_config.pixiv_illust_max_pages]:
        url = pixiv_client.strip_scale(raw_url)
        try:
            page = await client.download_image(
                url,
                blur=blur,
                radius=plugin_config.pixiv_illust_blur_radius,
                max_width=plugin_config.pixiv_illust_image_max_width,
            )
        except Exception as e:
            logger.warning(f"下载页图 {url} 失败: {type(e).__name__}: {e}")
            continue
        if page:
            pages.append(page)
    return pages


async def _build_illust_push(illust_id: int) -> tuple[Message, list[bytes]]:
    """拉详情 + 下载前 N 页 → `(文案+第 1 页图的消息, 剩余页)`。**纯取数，不负责表态**。

    ⚠️ **URL hook 不看 `pixiv_illust_r18_push_enabled`**：那条轴管的是「轮询时要不要把
    R18 新作推到群里」，而这里是用户自己贴的链接（明确要看的作品）——
    所以 R18 **照发**，只按打码开关处理画面（计划 §4.6）。
    """
    detail = await client.illust_detail(illust_id)
    x_restrict = int(getattr(detail, "x_restrict", 0) or 0)
    should_blur = bool(plugin_config.pixiv_illust_blur_r18 and x_restrict > 0)
    pages = await _fetch_pages(detail, blur=should_blur)
    # 一页都没下来就别声称「已模糊」—— 文案会自相矛盾
    blurred = should_blur and bool(pages)
    return message.build_push(detail, pages, blurred=blurred), pages[1:]


async def _send_push(event: MessageEvent, msg: Message, rest: list[bytes]) -> None:
    """发「文案 + 第 1 页」，再把第 2..N 页分流出去。

    ⚠️ 这里**只 send、不 finish**：`finish()` 抛的 FinishedException 属于 MatcherException，
    会把控制流直接带走 —— 终态表态就只能在它之前打，顺序又反了（用户要求「表态发生在消息
    发完之后」）。收尾的 finish 归 handler 管。

    私聊里没有「合并转发」这个概念，所以私聊一律多图一条（`_send_extra` 只服务群）。
    """
    await url_hook.send(msg)
    if not rest:
        return
    if isinstance(event, GroupMessageEvent):
        await _send_extra(event.group_id, rest)
        return
    extra = build_extra_pages(rest)
    if extra is not None:
        await url_hook.send(extra)


@with_reaction
async def _illust_flow(event: MessageEvent, illust_id: int) -> None:
    """拉详情 → 下载页图 → 发出去。三态表态（🔨 → 🍼/❌）由装饰器负责。

    ⚠️ **发送动作必须在被装饰的函数内**：装饰器只在函数返回/抛出时才打终态，
    而用户要求「表态发生在消息发完之后」。所以发送得放在这里。

    ⚠️ 失败时用 `send` 回文案后**再 `raise`**，不要用 `finish()`：
    `finish()` 抛的 `FinishedException` 属于 `MatcherException`，库会判成「成功」，
    于是出错反而显示 🍼。抛真异常才会让装饰器打 ❌。
    """
    try:
        msg, rest = await _build_illust_push(illust_id)
    except Exception:
        await url_hook.send(f"取插画 {illust_id} 失败，确认链接是否有效")
        raise

    await _send_push(event, msg, rest)


async def _poll_job() -> None:
    token = _resolve_refresh_token()
    if not token:
        logger.warning("refresh_token 为空（PIXIV_ILLUST_REFRESH_TOKEN 与全局 PIXIV_REFRESH_TOKEN 都没有），跳过轮询")
        return
    # token 可能是启动之后才配上的（或全局键改了）—— 与客户端里存的不一致时就同步过去，
    # 并丢掉旧的鉴权缓存（否则要等 TTL 到期才会用新 token 重新 auth）。
    if client.refresh_token != token:
        logger.info("refresh_token 变化，客户端下轮起用新 token 重新鉴权")
        client.refresh_token = token
        client._invalidate_api()
    try:
        n = await poll_once(client, plugin_config, send=_send, send_pages=_send_extra)
        if n:
            logger.info(f"本轮推送 {n} 条新作")
    except Exception as e:
        logger.exception(f"轮询异常: {type(e).__name__}: {e}")


@driver.on_startup
async def _startup() -> None:
    store.init(store.default_db_path())
    # 头像缓存放 localstore 的 cache 目录（重启不丢，不用重新下载）
    import nonebot_plugin_localstore as localstore

    avatars.init(localstore.get_plugin_cache_dir() / "avatars")
    logger.info(
        f"pixiv-illust 已启动：代理={plugin_config.pixiv_illust_proxy or '直连'} "
        f"间隔={plugin_config.pixiv_illust_poll_interval}s 订阅画师数={len(store.all_authors())} "
        f"仅管理员={plugin_config.pixiv_illust_admin_only} "
        f"R18推送={plugin_config.pixiv_illust_r18_push_enabled} "
        f"R18页图模糊={plugin_config.pixiv_illust_blur_r18}(radius={plugin_config.pixiv_illust_blur_radius}) "
        f"页图={plugin_config.pixiv_illust_image_max_width or '原图'} "
        f"最多页数={plugin_config.pixiv_illust_max_pages} "
        f"合并转发={'可用' if _mf_build_nodes else '不可用(降级多图)'} "
        f"群白名单={plugin_config.pixiv_illust_group_whitelist or '关闭(全放行)'} "
        f"群黑名单={plugin_config.pixiv_illust_group_blacklist or '关闭'} "
        f"URL被动={plugin_config.pixiv_illust_url_hook_enabled}(去重={plugin_config.pixiv_illust_url_hook_cooldown}s)"
    )
    if plugin_config.pixiv_illust_admin_only and not plugin_config.pixiv_illust_admin_ids:
        logger.info("PIXIV_ILLUST_ADMIN_IDS 未配置：管理员回退为 SUPERUSERS=%s", list(driver.config.superusers))

    if _resolve_refresh_token():
        scheduler.add_job(
            _poll_job,
            "interval",
            seconds=plugin_config.pixiv_illust_poll_interval,
            id="pixiv_illust_poll",
            replace_existing=True,
        )
    else:
        logger.warning(
            "refresh_token 为空（PIXIV_ILLUST_REFRESH_TOKEN 与全局 PIXIV_REFRESH_TOKEN 都没有）：只注册命令，不启动轮询"
        )


# ── 订阅插画（群聊限定：订阅是「按群」的）──────────────────────────
subscribe_cmd = on_command("订阅插画", priority=10, block=True)


@subscribe_cmd.handle()
async def _(event: GroupMessageEvent, args: Message = CommandArg()):
    # 群白名单在**管理员闸门之前**：不在白名单的群，管理员也不该能用 ——
    # 否则「白名单」就只限制了普通群友，等于没限制。
    if not _is_group_allowed(event):
        await subscribe_cmd.finish(GROUP_DENIED_MSG)

    if not _is_admin(event):
        await subscribe_cmd.finish(DENIED_MSG)

    # `finish()` 声明为 NoReturn（会抛 FinishedException），但 Pyright 不认
    # `await` 形式的 NoReturn 收窄，所以补一个显式 return 让控制流和类型都明确。
    author_id = handlers.extract_id(args.extract_plain_text())
    if author_id is None:
        await subscribe_cmd.finish("用法：订阅插画 <画师id>（也可直接粘画师主页链接）")
        return

    # 播种：取画师当前最新作品 ID，避免把历史作品一次性推出来
    baseline = 0
    author_name = ""
    author_avatar_url = ""
    author_fetch_failed = False
    if _resolve_refresh_token():
        try:
            illusts = await client.user_illusts(author_id)
            if illusts:
                baseline = max(int(i.id) for i in illusts)

            # 顺手把画师名/头像存下来，供订阅列表显示。
            # author_info 内部已容错：取不到就返回 ("", "")，不影响订阅本身。
            author_name, author_avatar_url = await client.author_info(author_id)
        except Exception as e:
            # 拉不到 = 画师 ID 很可能不存在（或代理挂了）。
            # 必须把这个事实带回回复里，否则用户看到「✅ 已订阅」却永远收不到推送。
            author_fetch_failed = True
            logger.warning(f"订阅时取画师 {author_id} 作品列表失败: {type(e).__name__}: {e}")

    await subscribe_cmd.finish(
        handlers.reply_subscribe(
            event.group_id,
            author_id,
            baseline,
            author_name=author_name,
            author_avatar_url=author_avatar_url,
            author_fetch_failed=author_fetch_failed,
        )
    )


# ── 退订插画（群聊限定）──────────────────────────────────────────
unsubscribe_cmd = on_command("退订插画", priority=10, block=True)


@unsubscribe_cmd.handle()
async def _(event: GroupMessageEvent, args: Message = CommandArg()):
    if not _is_group_allowed(event):
        await unsubscribe_cmd.finish(GROUP_DENIED_MSG)

    if not _is_admin(event):
        await unsubscribe_cmd.finish(DENIED_MSG)

    author_id = handlers.extract_id(args.extract_plain_text())
    if author_id is None:
        await unsubscribe_cmd.finish("用法：退订插画 <画师id>")
        return
    await unsubscribe_cmd.finish(handlers.reply_unsubscribe(event.group_id, author_id))


# ── 插画订阅列表（群聊限定；htmlkit 渲染成图片，失败回退纯文本）──────
list_cmd = on_command("插画订阅列表", priority=10, block=True)


@list_cmd.handle()
async def _(event: GroupMessageEvent):
    if not _is_group_allowed(event):
        await list_cmd.finish(GROUP_DENIED_MSG)

    if not _is_admin(event):
        await list_cmd.finish(DENIED_MSG)

    rows = store.list_by_group(event.group_id)
    if not rows:
        await list_cmd.finish(handlers.reply_list(event.group_id))  # 「还没有订阅」
        return

    # 先把头像下成 data URI（带 Referer + 磁盘缓存）。
    # 失败的头像不会进映射 → 模板显示首字占位块，**不影响整张图渲染**。
    try:
        avatar_map = await avatars.for_rows(rows, fetch=client.fetch_image)
    except Exception as e:
        logger.warning(f"批量取头像失败（改为无头像渲染）: {type(e).__name__}: {e}")
        avatar_map = {}

    img = await render.render_subscription_list(rows, group_id=event.group_id, avatars=avatar_map)
    if img is None:
        # 渲染不可用/失败 → 回退纯文本（render.py 里已记 warning）
        await list_cmd.finish(handlers.reply_list(event.group_id))
        return
    await list_cmd.finish(MessageSegment.image(img))


# ── 被动 URL hook：消息里出现 pixiv 插画/漫画链接就回「文案 + 前 N 页图」──
#
# 几个刻意的选择（都不是随手写的）：
#
# · `priority=20` + 命令是 `priority=10, block=True` → 命令先匹配并**阻断**传播，
#   所以「订阅插画 <画师主页链接>」只会走命令那一条路，**不会**再被这里回一次卡片。
#   不靠「文本里排除命令关键字」这种脆弱判断（COMMAND_START=[""] 时尤其容易漏）。
# · `block=False`：本 hook 是**旁观者**，绝不吞掉别人的消息 —— 别的插件
#   （如合并转发、词库）该收到还得收到。
# · 机器人自己的消息直接跳过，否则卡片里的 pixiv 链接会让它**自我触发**（无限循环）。
# · 不去查数据库、不看订阅关系：这个功能是「谁贴链接就答谁」，与订阅无关。
url_hook = on_message(priority=20, block=False)

# 去重窗口：键 = (会话, 作品ID) → 上次回复的 monotonic 时间
_seen: dict[tuple[str, int], float] = {}


def _session_key(event: MessageEvent) -> str:
    """会话标识：**按会话隔离**去重（群与群之间、群与私聊之间互不影响）。"""
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


def _should_skip(key: tuple[str, int]) -> bool:
    """同一会话里刚回过的同一个作品就跳过。返回 True 表示应跳过。"""
    window = plugin_config.pixiv_illust_url_hook_cooldown
    if window <= 0:
        return False
    now = time.monotonic()
    # 顺手清理过期项，避免长期运行下字典无限增长
    for k in [k for k, t in _seen.items() if now - t >= window]:
        _seen.pop(k, None)
    last = _seen.get(key)
    if last is not None and now - last < window:
        return True
    _seen[key] = now
    return False


@url_hook.handle()
async def _(event: MessageEvent):
    if not plugin_config.pixiv_illust_url_hook_enabled:
        return

    # 群白名单/黑名单在最前，且**静默 return**：
    # ① 被排除的群不该回卡片；② 更不该冒出 🔨/🍼 表情 ——
    #    所以表态必须在这个闸门**之内**（见下面的 `_illust_flow`），
    #    而不是用装饰器包住整个 handler（那样闸门一过就已经打了「处理中」）。
    if not _is_group_allowed(event):
        return

    # 机器人自己的消息（含它刚发出的卡片）—— 不跳过就会自我触发
    if str(event.user_id) == str(event.self_id):
        return

    # `urls.candidates` 连**分享卡片**（json 消息段）里的链接一起捞出来 ——
    # QQ 会把用户粘贴的链接转成卡片，这时纯文本里一个字都没有。
    illust_id = urls.parse(urls.candidates(event.get_message()))
    if illust_id is None:
        return

    key = (_session_key(event), illust_id)
    if _should_skip(key):
        logger.info(f"URL hook：{key} 在去重窗口内，跳过")
        return

    logger.info(f"URL hook 命中：illust={illust_id} 会话={key[0]}")

    # 🔨 表态、拉详情/下页图、发送都在 `_illust_flow` 里（它被 @with_reaction 包住）。
    # 只把「真正慢的那段」+ 发送放进去；校验/去重这些毫秒级判断留在外面。
    try:
        await _illust_flow(event, illust_id)
    except Exception as e:
        # 失败文案已经由 `_illust_flow` 发出（若连发送都失败则没有），
        # ❌ 也已由装饰器打过。这里只剩记录：再 finish 一次会多出一条消息。
        logger.warning(f"URL hook 处理 illust={illust_id} 结束于异常: {type(e).__name__}: {e}")

    # 收尾 finish：保持「这条消息已被本 hook 消费」的语义（block=False，
    # 不 finish 就会继续传给更低优先级的 matcher，别的插件可能会再解析一遍）。
    await url_hook.finish()
