"""从消息文本里认出 pixiv **插画 / 漫画**链接（被动 hook 用）。

只做纯文本解析，不碰网络、不碰 NoneBot —— 这样才可能把 URL 的各种变体
（语言前缀 / 多参数 / 锚点 / 短链）穷举着测一遍。

pixiv 三套链接形态并存，都要认：

- `pixiv.net/artworks/<id>` —— 现行主站链接；
- `pixiv.net/member_illust.php?...&illust_id=<id>` —— 老版链接；
- `pixiv.net/i/<id>` —— 老短链。

⚠️ 这里**刻意不用** `handlers.extract_id()`。那个函数取「第一个 3 位以上的数字」，
对 `pixiv.net/users/88871942`（画师主页）会返回 `88871942` 并当成**作品 ID** 去查
—— 变成「拿画师 ID 去查一个不存在的插画」，静默失败。
"""

from __future__ import annotations

import json
import re
from typing import Any

# pixiv 会给链接加语言前缀，如 /en/artworks/... 或 /zh-tw/artworks/...
_LANG = r"(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?"

_ARTWORK_RE = re.compile(rf"pixiv\.net/{_LANG}artworks/(\d+)", re.IGNORECASE)
_LEGACY_RE = re.compile(rf"pixiv\.net/{_LANG}member_illust\.php\?(?:[^\s#]*?&)?illust_id=(\d+)", re.IGNORECASE)
_SHORT_RE = re.compile(rf"pixiv\.net/{_LANG}i/(\d+)", re.IGNORECASE)

_PATTERNS: tuple[re.Pattern[str], ...] = (_ARTWORK_RE, _LEGACY_RE, _SHORT_RE)


def parse(text: str) -> int | None:
    """消息里**最靠前**的插画链接的 id；没有则 None（多条只取最早的，防刷屏）。

    为什么只取一个：一条消息里贴三四个链接时，连发三四张卡片会刷屏。
    取「最靠前」而不是「先匹配 artworks 再看老链接」—— 后者在
    「先说老链接再说新链接」的消息里会答错顺序。

    为什么带 `[^\\s#]*?&`：`member_illust.php?mode=medium&illust_id=900` 的
    `illust_id` 不在第一个参数位。`#` 排除掉锚点（`#m1` 是第几页的标记）。
    """
    if not text:
        return None
    best: tuple[int, int] | None = None
    for rx in _PATTERNS:
        m = rx.search(text)
        if m is not None and (best is None or m.start() < best[0]):
            best = (m.start(), int(m.group(1)))
    if best is None:
        return None
    return best[1]


def candidates(message: Any) -> str:
    """把**整条消息**拼成一段可搜文本，用于认链接。

    为什么不只用 `event.get_plaintext()`：QQ 会把用户粘贴的链接转成
    **分享卡片**（一段 `json` 消息段），这时纯文本里**一个字都没有**，
    链接只存在于 json 段的数据里 —— 只用纯文本会漏掉这大半数情况。

    非 text 段用 `json.dumps` 序列化：Python 默认**不转义 `/`**，
    所以 URL 保持原样可匹配（CQ 码那种 `\\/` 转义形式反而会失配）。

    接受任何「可迭代 + 每项有 .type/.data」的对象（NoneBot 的 Message 就是），
    也接受纯字符串，便于单测。
    """
    if isinstance(message, str):
        return message
    parts: list[str] = []
    for seg in message or []:
        seg_type = getattr(seg, "type", "")
        data = getattr(seg, "data", None) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        else:
            try:
                parts.append(json.dumps(data, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                continue
    return "\n".join(parts)


def parse_all(text: str) -> list[int]:
    """消息里**全部**插画链接的 id，按出现顺序、已去重。给日志与「只回第一条」的提示用。"""
    if not text:
        return []
    hits: list[tuple[int, int]] = []
    for rx in _PATTERNS:
        hits.extend((m.start(), int(m.group(1))) for m in rx.finditer(text))
    hits.sort()

    out: list[int] = []
    for _, illust_id in hits:
        if illust_id not in out:
            out.append(illust_id)
    return out
