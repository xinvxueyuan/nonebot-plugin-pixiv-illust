"""merge-forward 分流：≤3 直发多图 / >3 合并转发 / 库缺失降级。

用户 2026-10-14 拍板的推送形态（计划 §4.4）：作品第 2..N 页
  - ≤3 张 → 一条多图消息（`message.build_extra_pages`）；
  - >3 张 → 合并转发（`build_nodes` + `send_group_forward_msg`，与次元图插件同款）；
  - 合并转发库缺失 → **降级**成多图一条 + warning，**绝不因此炸插件**。

这三条各有一个变异靶子：把阈值改成 `>=`、把转发分支删掉、把降级写成 `raise` —— 都会变红。
"""

import base64

import pytest
from test_poller import _multi_page

pytest.importorskip("nonebot")

import nonebot_plugin_pixiv_illust as plugin
from nonebot.adapters.onebot.v11 import Message

pytest.importorskip(
    "nonebot_plugin_merge_forward",
    reason="合并转发库是可选依赖，拿不到它的源码时跳过（降级路径另有测试）",
)


class FakeBot:
    """只记下「发了什么」，不连任何协议端。"""

    def __init__(self, self_id: str = "10000") -> None:
        self.self_id = self_id
        self.group_msgs: list[tuple[int, Message]] = []
        self.forward_msgs: list[tuple[int, list]] = []

    async def send_group_msg(self, *, group_id, message):
        self.group_msgs.append((group_id, message))
        return {"message_id": 1}

    async def send_group_forward_msg(self, *, group_id, messages):
        self.forward_msgs.append((group_id, messages))
        return {"message_id": 2}


@pytest.fixture
def bot(monkeypatch) -> FakeBot:
    fake = FakeBot()
    monkeypatch.setattr(plugin, "get_bot", lambda: fake)
    return fake


def _images(msg) -> int:
    return len([seg for seg in msg if seg.type == "image"])


def _seg_bytes(seg) -> bytes:
    """从 image 段里取回原始字节。

    两种形状都要认：`MessageSegment`（`build_extra_pages` 的产物）与
    merge-forward 的 `to_v11_content` 转出来的**裸 dict**（`{"type","data"}`）。
    """
    data = seg["data"] if isinstance(seg, dict) else seg.data
    return base64.b64decode(str(data["file"]).removeprefix("base64://"))


def test_the_library_is_actually_loaded():
    """**假绿的守门员**。

    库拿不到时插件的两个全局都是 None，于是「>3 页走合并转发」的用例
    会静默退到降级路径、**照样通过**。这条把「到底加载到没有」钉死。
    """
    assert plugin._mf_build_nodes is not None, "merge-forward 没加载上，下面的用例测的不是转发路径"
    assert plugin._mf_send_forward is not None


# ── ① ≤3 张：一条多图消息 ─────────────────────────────────────────


async def test_three_pages_go_as_one_multi_image_message(bot):
    await plugin._send_extra(868258211, [b"p2", b"p3", b"p4"])

    assert bot.forward_msgs == [], "3 张不该走合并转发"
    assert len(bot.group_msgs) == 1, "剩余的 3 张要装在**一条**消息里"
    gid, msg = bot.group_msgs[0]
    assert gid == 868258211
    assert _images(msg) == 3
    # 真调了 `build_extra_pages`：比对**内容**（每张图的字节要一模一样）
    assert [_seg_bytes(seg) for seg in msg] == [b"p2", b"p3", b"p4"]


async def test_one_page_also_goes_as_a_plain_message(bot):
    """2 页作品（只有第 2 页剩余）也走直发，不是转发。"""
    await plugin._send_extra(868258211, [b"p2"])

    assert bot.forward_msgs == []
    assert _images(bot.group_msgs[0][1]) == 1


# ── ② >3 张：合并转发 ─────────────────────────────────────────────


async def test_four_pages_go_as_a_merged_forward(bot):
    """>3 张交给 `build_nodes` + `send_group_forward_msg`（**真库**，不是替身）。"""
    await plugin._send_extra(868258211, [b"p2", b"p3", b"p4", b"p5"])

    assert bot.group_msgs == [], "走了合并转发就不要再直发一条多图"
    assert len(bot.forward_msgs) == 1
    gid, nodes = bot.forward_msgs[0]
    assert gid == 868258211
    assert len(nodes) == 4, f"每页一个 node，期望 4 个，实际 {len(nodes)}"
    assert all(node["type"] == "node" for node in nodes)
    assert {node["data"]["nickname"] for node in nodes} == {"pixiv 插画"}
    assert {node["data"]["user_id"] for node in nodes} == {bot.self_id}
    # 每个 node 里正好一张图，字节与传入的页图一致
    assert [_seg_bytes(node["data"]["content"][0]) for node in nodes] == [b"p2", b"p3", b"p4", b"p5"]


# ── ③ 库缺失 / 转发失败：降级多图一条 ─────────────────────────────


async def test_missing_library_degrades_to_one_multi_image_message(bot, monkeypatch):
    """库缺失（加载期 try 失败 → 两个全局都是 None）：降级多图一条，不炸。"""
    monkeypatch.setattr(plugin, "_mf_build_nodes", None)
    monkeypatch.setattr(plugin, "_mf_send_forward", None)

    await plugin._send_extra(868258211, [b"p2", b"p3", b"p4", b"p5"])

    assert bot.forward_msgs == []
    assert len(bot.group_msgs) == 1
    assert _images(bot.group_msgs[0][1]) == 4


async def test_forward_failure_also_degrades_instead_of_losing_the_pages(bot, monkeypatch):
    """运转发失败（协议端不支持 / 网络抖动）：退回多图一条，而不是把这 4 页丢了。"""

    async def boom(*args, **kwargs):
        raise RuntimeError("协议端不支持合并转发")

    monkeypatch.setattr(plugin, "_mf_send_forward", boom)

    await plugin._send_extra(868258211, [b"p2", b"p3", b"p4", b"p5"])

    assert bot.forward_msgs == []
    assert len(bot.group_msgs) == 1
    assert _images(bot.group_msgs[0][1]) == 4


# ── 端到端：URL hook 的推送形态（文案+第 1 页，剩余页走分流）─────────


async def test_illust_flow_splits_multi_page_works_into_forward(bot, monkeypatch):
    """5 页作品：第 1 页随文案（走 matcher），第 2..5 页（4 张 > 3）走合并转发。"""
    from nonebot_plugin_pixiv_illust import _illust_flow, client, plugin_config, url_hook

    detail = _multi_page(29373865, 5)

    async def fake_detail(_illust_id):
        return detail

    async def fake_page(url, *, blur, radius, max_width=0):
        return f"page:{url.rsplit('/', 1)[-1]}".encode()

    sent: list[Message] = []
    monkeypatch.setattr(plugin_config, "pixiv_illust_max_pages", 5)  # 全部 5 页都要下下来
    monkeypatch.setattr(client, "illust_detail", fake_detail)
    monkeypatch.setattr(client, "download_image", fake_page)

    async def fake_send(message, **kwargs):
        sent.append(message)
        return {"message_id": 1}

    monkeypatch.setattr(url_hook, "send", fake_send)

    await _illust_flow(_group_event(), 29373865)

    assert len(sent) == 1, "第 1 页随文案发（文案里只有 1 张图）"
    assert _images(sent[0]) == 1
    assert "共 5 页，本条发送前 5 页" in str(sent[0])

    assert bot.group_msgs == []
    assert len(bot.forward_msgs) == 1
    assert len(bot.forward_msgs[0][1]) == 4


def _group_event():
    """真实 `GroupMessageEvent` 替身（`_send_push` 用 `isinstance` 判群聊/私聊）。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    return GroupMessageEvent.model_construct(group_id=868258211, user_id=1330509996)
