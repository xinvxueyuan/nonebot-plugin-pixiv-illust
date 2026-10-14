"""订阅列表的图片渲染（nonebot-plugin-htmlkit）。

四个设计要点：
  1. **惰性 import**：htmlkit 是 C++ 扩展，且 README 要求先 `require()` 再 import。
     把它放进函数里 try/except —— 万一某台机器加载不了，**不能因此让整个插件加载失败**。
  2. **任何失败都返回 None**，由调用方回退到 `handlers.reply_list()` 的纯文本。
  3. CSS **内联在模板里**，不依赖 base_url 去解析外部 css 资源。
  4. **头像以 data URI 传入**（不是 http URL）：htmlkit 取网络图不带 Referer，
     而 i.pximg.net 不带 Referer 就 403。data URI 走 native_data_scheme 原生解码（见 §2.5）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .message import user_url

logger = logging.getLogger("nonebot_plugin_pixiv_illust")

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "subscription_list.html"
MAX_ITEMS = 50  # 模板最多渲染多少条，避免生成超长图


def _fmt_time(ts: Any) -> str:
    """unix 秒 → `YYYY-MM-DD HH:MM`（本地时区）。取不到就给空串。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """从订阅行里取值，**同时兼容 `sqlite3.Row` 和 dict**。

    ⚠️⚠️ `sqlite3.Row` **没有 `.get()`**（只有 `row["key"]`，缺键会抛 IndexError）。
    而 `store` 设了 `conn.row_factory = sqlite3.Row`，所以生产环境的行**全是 Row**。

    直接写 `r.get("author_name")` 会抛
    `AttributeError: 'sqlite3.Row' object has no attribute 'get'`；
    而 `render_subscription_list()` 把**所有**异常都吞成「回退纯文本」，
    于是「订阅列表」的图片渲染**整个是死的、还不报错** —— 用户只看到永远是纯文本。

    这个 bug 单测没抓到，因为测试里的行是手写的 **dict**（有 `.get`）。
    现在 `tests/test_render.py` 改成从真 store 取行，形状与生产一致。
    """
    if hasattr(row, "get"):  # dict / Mapping
        return row.get(key, default)
    try:  # sqlite3.Row
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def build_context(
    rows: Sequence[Any],
    *,
    group_id: int,
    max_items: int = MAX_ITEMS,
    avatars: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """把 store 的行转成模板上下文。**纯函数**，可单测。

    `avatars` 是 `{头像URL: dataURI}`（由 `avatars.for_rows()` 预先取好）。
    映射里没有的（下载失败 / URL 为空）→ `avatar_uri=None`，模板退化成首字占位块。
    """
    avatars = avatars or {}
    items = []
    for i, r in enumerate(rows[:max_items]):
        author_id = int(r["author_id"])
        name = str(_row_get(r, "author_name") or "").strip()
        avatar_url = str(_row_get(r, "author_avatar_url") or "")
        items.append(
            {
                "index": i + 1,
                "author_id": author_id,
                "author_name": name or str(author_id),  # 没名字就显示 ID，不留白
                "author_url": user_url(author_id),
                "avatar_uri": avatars.get(avatar_url) or None,
                "subscribed_at": _fmt_time(_row_get(r, "created_at")),
                "last_seen": int(r["last_seen"]),
            }
        )
    total = len(rows)
    return {
        "group_id": group_id,
        "count": total,
        "items": items,
        "truncated": max(total - max_items, 0),
    }


def _load_htmlkit() -> Callable[..., Any]:
    """惰性取出 `template_to_pic`。

    单独抽成一个函数，是为了让单测能 monkeypatch 它 —— 否则没装 htmlkit 就跑不了单测。
    """
    from nonebot_plugin_htmlkit import template_to_pic

    return template_to_pic


async def render_subscription_list(
    rows: Sequence[Any],
    *,
    group_id: int,
    avatars: Mapping[str, str] | None = None,
    max_width: int = 860,
) -> bytes | None:
    """把订阅列表渲染成 PNG。**任何失败都返回 None**（调用方负责回退纯文本）。

    `max_width` 是**上限（cap）**，不是缩放器。`allow_refit=True` 时内容会被
    挤压去适配它 —— 所以 CSS 里 `.card` 的宽度 + 两侧 padding 必须**小于**这个值，
    否则会被压扁、文字溢出卡片（实测踩过：卡片 700px + body padding 40 时
    总宽 740 > 上限 700，压完之后内容冲出卡片右缘 38px）。

    当前模板：`.card` 760px + body padding 20*2 = 总宽 800，所以这里给 860 留余量。
    改模板宽度时记得同步核对这个值。
    """
    if not rows:
        return None
    try:
        template_to_pic = _load_htmlkit()
        context = build_context(rows, group_id=group_id, avatars=avatars)
        return await template_to_pic(
            TEMPLATES_DIR,  # 位置参数 1：模板环境路径
            TEMPLATE_NAME,  # 位置参数 2：模板名
            context,  # 位置参数 3：模板参数（**参数名是 templates，复数**）
            dpi=96.0,
            max_width=max_width,
            device_height=600,
            allow_refit=True,
            image_format="png",
        )
    except Exception as e:
        logger.warning(f"订阅列表渲染失败，回退纯文本: {type(e).__name__}: {e}")
        return None
