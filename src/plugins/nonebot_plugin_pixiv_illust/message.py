"""把 pixiv 插画/漫画拼成 OneBot V11 消息（文案 + 分页图 + 直发/合并转发分流）。"""

from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

_R18_LABEL = {1: "R18", 2: "R18G"}

# 剩余页 ≤3：一条多图消息直发；>3：交给合并转发（用户 2026-10-14 拍板）。
DIRECT_PAGES_LIMIT = 3


def artwork_url(artwork_id: int | str) -> str:
    return f"https://www.pixiv.net/artworks/{artwork_id}"


def user_url(user_id: int | str) -> str:
    return f"https://www.pixiv.net/users/{user_id}"


def _tag_names(illust: Any) -> list[str]:
    """兼容 tags 的三种形态：IllustTag 对象（`.name`）、dict（网页接口的 `tag` 键）、纯 str。"""
    out: list[str] = []
    for t in getattr(illust, "tags", None) or []:
        if isinstance(t, str):
            name: Any = t
        elif isinstance(t, dict):
            name = t.get("name") or t.get("tag")
        else:
            name = getattr(t, "name", None)
        if name:
            out.append(str(name))
    return out


def _r18_line(x_restrict: Any, blurred: bool) -> str | None:
    """R18 标记行；非 R18 返回 None。"""
    xr = int(x_restrict or 0)
    if xr not in _R18_LABEL:
        return None
    suffix = "（已模糊）" if blurred else "（未模糊）"
    return f"🔞 {_R18_LABEL[xr]}{suffix}"


def build_push(illust: Any, pages: list[bytes], *, blurred: bool) -> Message:
    """文案 + 第 1 页图（同一条消息）。

    Args:
        illust: pixivpy3 的 IllustInfo / 等价对象
        pages: 已下载处理好的页图字节（第 1..N 页，调用方切好；可能只有 1 页或为空）
        blurred: 页图是否已做过模糊（仅影响 R18 文案后缀）
    """
    lines = [f"🖼 {illust.title}", f"✍️ {illust.user.name} · {user_url(illust.user.id)}"]

    tags = _tag_names(illust)
    if tags:
        lines.append("🏷 " + " ".join(tags))

    r18 = _r18_line(getattr(illust, "x_restrict", 0), blurred)
    if r18:
        lines.append(r18)

    total = int(getattr(illust, "page_count", len(pages)) or 1)
    if total > 1:
        lines.append(f"📄 共 {total} 页，本条发送前 {min(len(pages), total)} 页")

    lines.append(f"🔗 {artwork_url(illust.id)}")

    msg = Message(MessageSegment.text("\n".join(lines)))
    if pages:
        msg += MessageSegment.image(pages[0])
    return msg


def pages_need_forward(count: int) -> bool:
    """剩余页数量是否需要走合并转发（≤3 直发多图，>3 转发）。"""
    return count > DIRECT_PAGES_LIMIT


def build_extra_pages(rest: list[bytes]) -> Message | None:
    """剩余页（第 2..N 页，调用方已切好）装成**一条多图消息**；没有剩余页返回 None。

    >3 页时不走这里 —— 由 __init__ 的 `_send_extra` 改走合并转发（见计划阶段 10）。
    """
    if not rest:
        return None
    msg = Message()
    for p in rest:
        msg += MessageSegment.image(p)
    return msg
