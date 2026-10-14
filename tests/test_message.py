"""message.py：推送文案行序、R18/页数行、分页图与「直发/合并转发」分流。

用户 2026-10-14 拍板：剩余页 ≤3 直发多图；>3 走合并转发。
"""

import base64
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_pixiv_illust.message import (
    DIRECT_PAGES_LIMIT,
    artwork_url,
    build_extra_pages,
    build_push,
    pages_need_forward,
    user_url,
)


@dataclass
class FakeTag:
    name: str
    translated_name: str | None = None


@dataclass
class FakeUser:
    id: int = 42
    name: str = "画师甲"


@dataclass
class FakeIllust:
    id: int = 900
    title: str = "测试插画"
    x_restrict: int | None = 0
    page_count: int = 1
    tags: Any = field(default_factory=lambda: [FakeTag("タグA"), FakeTag("タグB")])
    user: FakeUser = field(default_factory=FakeUser)


def _text(msg: Message) -> str:
    """第 0 段固定是文案（text 段），取原文精确比对行序。"""
    assert msg[0].type == "text"
    return str(msg[0].data["text"])


def _payload(seg: Any) -> bytes:
    """image 段里内嵌的图片字节（OneBot V11 会把 bytes 转成 base64://）。"""
    file = str(seg.data["file"])
    assert file.startswith("base64://"), f"image 段不是内嵌字节: {file[:32]}"
    return base64.b64decode(file[len("base64://") :])


# ── URL 与分流常量 ────────────────────────────────────────────────


def test_urls():
    assert artwork_url(900) == "https://www.pixiv.net/artworks/900"
    assert user_url(42) == "https://www.pixiv.net/users/42"


def test_direct_pages_limit_is_three():
    assert DIRECT_PAGES_LIMIT == 3


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, False), (1, False), (2, False), (3, False), (4, True), (10, True)],
)
def test_pages_need_forward(count: int, expected: bool):
    assert pages_need_forward(count) is expected


# ── build_extra_pages：剩余页（第 2..N 页）装成一条多图消息 ────────


def test_extra_pages_empty_returns_none():
    assert build_extra_pages([]) is None


def test_extra_pages_packs_all_remaining_into_one_message():
    msg = build_extra_pages([b"p2", b"p3", b"p4"])
    assert isinstance(msg, Message)
    assert [seg.type for seg in msg] == ["image", "image", "image"]
    # 顺序即第 2..N 页，不许乱序/去重
    assert [_payload(seg) for seg in msg] == [b"p2", b"p3", b"p4"]


def test_extra_pages_is_unbounded_because_limit_lives_in_pages_need_forward():
    """直发/转发的分界由调用方用 pages_need_forward 判，本函数只负责打包。"""
    msg = build_extra_pages([b"p2", b"p3", b"p4", b"p5", b"p6"])
    assert msg is not None and len(msg) == 5


# ── build_push：文案行序 + 第 1 页图 ──────────────────────────────


def test_push_full_line_order():
    illust = FakeIllust(x_restrict=1, page_count=5)
    msg = build_push(illust, [b"p1", b"p2"], blurred=True)
    text = _text(msg)
    assert text == "\n".join(
        [
            "🖼 测试插画",
            "✍️ 画师甲 · https://www.pixiv.net/users/42",
            "🏷 タグA タグB",
            "🔞 R18（已模糊）",
            "📄 共 5 页，本条发送前 2 页",
            "🔗 https://www.pixiv.net/artworks/900",
        ]
    )


def test_push_first_page_rides_along_and_only_first():
    msg = build_push(FakeIllust(page_count=3), [b"p1", b"p2", b"p3"], blurred=False)
    assert [seg.type for seg in msg] == ["text", "image"]
    assert _payload(msg[1]) == b"p1"


def test_push_without_pages_is_text_only():
    msg = build_push(FakeIllust(), [], blurred=False)
    assert [seg.type for seg in msg] == ["text"]


# ── 标签行：有才加，对象/字符串/字典三态都认 ─────────────────────


def test_tags_line_absent_when_no_tags():
    assert "🏷" not in _text(build_push(FakeIllust(tags=[]), [], blurred=False))


def test_tags_accept_objects_strings_and_dicts():
    illust = FakeIllust(tags=[FakeTag("对象"), "字符串", {"name": "字典"}, {"tag": "tag键"}])
    text = _text(build_push(illust, [], blurred=False))
    assert "🏷 对象 字符串 字典 tag键" in text


# ── R18 行：x_restrict>0 才加，带模糊后缀 ────────────────────────


def test_r18_line_absent_for_non_r18():
    assert "🔞" not in _text(build_push(FakeIllust(x_restrict=0), [], blurred=True))


def test_r18_line_absent_when_x_restrict_missing():
    assert "🔞" not in _text(build_push(FakeIllust(x_restrict=None), [], blurred=False))


def test_r18_blurred_suffix():
    text = _text(build_push(FakeIllust(x_restrict=1), [], blurred=True))
    assert "🔞 R18（已模糊）" in text


def test_r18_unblurred_suffix():
    text = _text(build_push(FakeIllust(x_restrict=1), [], blurred=False))
    assert "🔞 R18（未模糊）" in text


def test_r18g_label():
    assert "🔞 R18G（未模糊）" in _text(build_push(FakeIllust(x_restrict=2), [], blurred=False))


# ── 页数行：page_count>1 才加 ────────────────────────────────────


def test_page_line_absent_for_single_page():
    assert "📄" not in _text(build_push(FakeIllust(page_count=1), [b"p1"], blurred=False))


def test_page_line_counts_total_and_sent():
    text = _text(build_push(FakeIllust(page_count=9), [b"p1", b"p2", b"p3"], blurred=False))
    assert "📄 共 9 页，本条发送前 3 页" in text


def test_page_line_falls_back_to_downloaded_count_without_page_count():
    illust = SimpleNamespace(
        id=1,
        title="无字数插画",
        x_restrict=0,
        tags=[],
        user=SimpleNamespace(id=2, name="画师乙"),
    )  # 刻意不给 page_count 属性
    text = _text(build_push(illust, [b"p1", b"p2"], blurred=False))
    assert "📄 共 2 页，本条发送前 2 页" in text


def test_page_count_zero_treated_as_one():
    assert "📄" not in _text(build_push(FakeIllust(page_count=0), [b"p1"], blurred=False))


# ── 链接行：永远带 ───────────────────────────────────────────────


def test_link_always_last_regardless_of_optional_lines():
    text = _text(build_push(FakeIllust(x_restrict=0, tags=[], page_count=1), [], blurred=True))
    assert text.splitlines()[-1] == "🔗 https://www.pixiv.net/artworks/900"
