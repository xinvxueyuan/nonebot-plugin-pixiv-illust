"""命令的业务逻辑。与 NoneBot 解耦，方便单测。

**与参照仓（pixiv 小说）的差异**：插画/漫画没有「全文」概念 —— 图片由 URL hook 与
轮询直接发出去，所以参照仓的 `deliver_novel_text`（内联/文件/链接三级降级）与
`safe_filename`（txt 文件名清洗）**整体删除**；这里只留纯文案与 ID 提取。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import store
from .message import user_url

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

# 至少 3 位：防手滑把「第 12 张」之类的碎数字当成作品/画师 id。
_ID_RE = re.compile(r"(\d{3,})")


def extract_id(text: str | None) -> int | None:
    """从参数里取出 ID：支持纯数字，也支持直接粘 pixiv 链接。"""
    if not text:
        return None
    m = _ID_RE.search(text)
    return int(m.group(1)) if m else None


def reply_message_id(event: Any) -> int | None:
    """取本条消息**引用**的那条消息的 id；没引用或取不到返回 None。

    URL hook 用它来判断「这条消息是不是在回别人的链接」——引用时不重复贴同一张图，
    只补一句已经发过了。

    为什么要防御式地读：`event.reply` 的填充方式**按 OneBot 实现而异** ——
    它可能是适配器 pydantic 模型里的 `Reply`（字段 `message_id`），
    也可能被实现塞成一个 `type="reply"` 的 `MessageSegment`（数据在 `data["id"]`）。
    写死其中一种，换个实现就静默返回 None（症状是「明明引用了消息但说没引用」）。

    ⚠️ `id` 可能是 `"0"` 或空串（实现找不到原消息时的占位），这类值一律当没引用 ——
    否则下游会拿 0 当真 id 用，用户看到的是「引用的消息我不认识」，
    而真实原因是「这条消息根本没引用成功」，两种情况的处置完全不同。
    """
    reply = getattr(event, "reply", None)

    raw: Any = None
    if reply is not None:
        data = getattr(reply, "data", None)
        if isinstance(data, dict):
            raw = data.get("id")
        if raw is None:
            raw = getattr(reply, "message_id", None)

    # 退回扫消息段：适配器没填 event.reply 时，reply 段可能只存在于消息体里。
    if raw is None:
        getter = getattr(event, "get_message", None)
        if callable(getter):
            try:
                message: Any = getter()
                for seg in message:
                    if getattr(seg, "type", None) == "reply":
                        raw = (getattr(seg, "data", None) or {}).get("id")
                        break
            except Exception:
                logger.warning("扫描引用消息段失败", exc_info=True)

    if raw is None:
        return None
    try:
        mid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return mid or None


def _author_label(row_or_id: Any, name: str = "") -> str:
    """画师显示名：显式传入的名字 → 行里的 `author_name` → 退回 ID。

    ⚠️ 行是 `sqlite3.Row`，**不是 dict** —— 只判 `isinstance(..., dict)` 会让它掉到
    `str(row)`，回复里就出现 `<sqlite3.Row object at 0x...>`。用 `keys()` 判「像不像行」。
    """
    explicit = (name or "").strip()
    if explicit:
        return explicit

    if hasattr(row_or_id, "keys"):
        row_name = str(row_or_id["author_name"] or "").strip()
        return row_name or str(row_or_id["author_id"])

    return str(row_or_id)


def reply_subscribe(
    group_id: int,
    author_id: int,
    baseline: int,
    author_name: str = "",
    author_avatar_url: str = "",
    author_fetch_failed: bool = False,
) -> str:
    """订阅。`author_name` / `author_avatar_url` **允许为空**（API 失败时不阻塞订阅）。

    名字和头像会被存进表里，供订阅列表显示。

    `author_fetch_failed=True`（有 token 但拉取画师信息失败）时**必须**在回复里警告 ——
    否则用户把画师 ID 打错也会看到「✅ 已订阅」，然后永远收不到任何推送、还找不到原因。
    """
    created = store.subscribe(
        group_id,
        author_id,
        baseline=baseline,
        author_name=author_name,
        author_avatar_url=author_avatar_url,
    )
    label = _author_label(author_id, author_name)
    if not created:
        return f"本群已经订阅过画师 {label} 了\n{user_url(author_id)}"

    warn = ""
    if author_fetch_failed:
        warn = (
            f"\n⚠️ 但**没能从 pixiv 拉到该画师的信息** —— 画师 ID 可能有误（或网络/代理异常）。\n"
            f"订阅已记录，但如果是无效 ID，之后不会有任何推送。"
            f"建议发 `退订插画 {author_id}` 后重新确认 ID。"
        )
    return f"✅ 已订阅画师 {label}\n{user_url(author_id)}\n从现在开始推送新作（订阅前的历史作品不推送）{warn}"


def reply_unsubscribe(group_id: int, author_id: int) -> str:
    if store.unsubscribe(group_id, author_id):
        return f"✅ 已退订画师 {author_id}"
    return f"本群没有订阅画师 {author_id}"


def reply_list(group_id: int) -> str:
    """订阅列表的**纯文本**版本。

    这是订阅列表出图失败时的回退，所以必须始终可用（不依赖 htmlkit / 网络）。
    """
    rows = store.list_by_group(group_id)
    if not rows:
        return "本群还没有订阅任何画师"
    lines = ["本群订阅的画师："]
    lines += [f"{i + 1}. {_author_label(r)}  {user_url(r['author_id'])}" for i, r in enumerate(rows)]
    return "\n".join(lines)
