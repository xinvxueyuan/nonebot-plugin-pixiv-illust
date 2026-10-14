"""表态接线：三态（处理中 / 完成 / 失败）且**终态发生在消息发出之后**。

用户两条明确要求（2026-10-02）：
  ① 慢操作要有 🔨/🍼/❌ 三态反馈；
  ② **成功表态移到发完消息之后**，随后追加「**失败表态也移到发完消息之后**」。

② 不是锦上添花 —— 它决定了代码结构：`with_reaction` 只在函数返回/抛出时才打终态，
所以「回文案/发卡片」的动作必须**在被装饰的函数内部**。把发送留在外面
（无论 handler 还是独立小函数）都会退化成「先 ❌ 再回文案」。
本文件里带「顺序」字样的用例就是钉这件事的。

**对参照仓（pixiv 小说）用例的取舍**：本仓没有「获取全文」这条链路，也没有卡片映射表
（`card_message`），所以小说仓那两个 `_deliver_full_text` 用例、`db` fixture、
以及「正文交付阶段才失败」的用例**不适用**，未移植；被动 hook 那条改成 `_illust_flow`，
「表态不该扩散到纯取数函数」那条改成 `_build_illust_push`。

边界：**库内部正确性由库自己的测试负责**（EMOJI_MAP 取值、私聊跳过、
MatcherException 判成功等）。这里只验证插件的接线。
"""

from types import SimpleNamespace

import pytest
import source_introspect
from conftest import wrap_json
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from test_poller import FakeIllust

reactlib = pytest.importorskip(
    "nonebot_plugin_message_reaction",
    reason="表态库是可选依赖，拿不到它的源码时跳过（插件降级路径另有测试）",
)

from nonebot_plugin_pixiv_illust import (  # noqa: E402
    _illust_flow,
    reaction_available,
)


def _single_page_illust(illust_id: int = 29373865) -> FakeIllust:
    """单页作品（真实形状：`meta_pages=[]` → 退化到 `image_urls.large`）。

    单页很关键：`_send_push` 的剩余页分流会去要 `get_bot()`，
    而本文件测的是**表态顺序**，不该被「有没有 bot 连着」这件事干扰。
    """
    return FakeIllust(illust_id, title="单页插画", page_count=1)


def _group_event():
    """真实 `GroupMessageEvent` 的替身构造。

    **不能用 SimpleNamespace**：插件里判群聊/私聊走的是
    `isinstance(event, GroupMessageEvent)`，鸭子类型的假事件会被判成私聊 ——
    于是 `_send_push` 会走「私聊多图」那条分支，测试结论全是错的。

    `model_construct` 绕开 pydantic 的必填字段校验（真实事件有十几个字段），
    但拿到的仍是**真类实例**，`isinstance` 与 `get_user_id()` 都正常。
    """
    return GroupMessageEvent.model_construct(group_id=1094538078, user_id=1330509996)


@pytest.fixture
def use_event(monkeypatch):
    """把「当前事件」喂给库。

    ⚠️ 不能用 `current_event.set(event)` + reset：`ContextVar` 的 token 只能在
    创建它的上下文里 reset，而 sync fixture 与 async 测试体不是同一个上下文
    （会 `ValueError: token was created in a different Context`）。
    直接替换库模块上的对象最省事，也正好覆盖「库从模块全局取它」这一事实。

    不替换的话，`with_reaction` 会走 `LookupError` 分支**静默跳过所有表态**，
    于是「表态有没有发出」的断言全部落空（看起来通过，其实什么都没测）。
    """

    class _Current:
        def __init__(self, event):
            self._event = event

        def get(self):
            return self._event

    def _use(event):
        monkeypatch.setattr(reactlib.reaction, "current_event", _Current(event))
        return event

    return _use


@pytest.fixture
def timeline(monkeypatch):
    """**同一条时间线**记录表态与发送，用于断言先后顺序。

    这是本组测试的核心装置：分成两个列表就只能断言「都发生了」，
    断言不了「谁先谁后」—— 而用户要的恰恰是顺序。
    """
    events: list[str] = []
    holder = SimpleNamespace(events=events)

    async def fake_reaction(event, status):
        events.append(f"表态:{status}")
        return True

    # 打的是**库模块**的属性（而不是插件的导入名）—— `with_reaction` 在调用时
    # 才从模块全局取 `message_reaction`，所以这样能拦住所有被装饰的函数。
    monkeypatch.setattr(reactlib.reaction, "message_reaction", fake_reaction)

    def patch_send(matcher, label, response=None):
        """拦 `matcher.send()`，把「发了什么」记进同一条时间线。

        替身是**普通函数**（不是 classmethod）也没关系：通过类访问时不会被绑定，
        `matcher.send(msg)` 正好只传一个参数。
        """

        async def fake_send(message, **kwargs):
            events.append(f"发送:{label}")
            return response

        monkeypatch.setattr(matcher, "send", fake_send)

    holder.patch_send = patch_send
    return holder


def _patch_client(monkeypatch, *, detail=None, page=None):
    """把真实 `PixivClient` 的两个方法换成替身（不发任何网络请求）。"""
    from nonebot_plugin_pixiv_illust import client

    async def fake_detail(illust_id):
        if isinstance(detail, Exception):
            raise detail
        return detail if detail is not None else _single_page_illust(illust_id)

    async def fake_page(url, *, blur, radius, max_width=0):
        if isinstance(page, Exception):
            raise page
        return page if page is not None else b"\x89PNG\r\n\x1a\n"

    monkeypatch.setattr(client, "illust_detail", fake_detail)
    monkeypatch.setattr(client, "download_image", fake_page)


# ── 插件确实加载到了真库，而不是降级替身 ──────────────────────────


def test_plugin_actually_loaded_the_real_library():
    """这条是**假绿的守门员**。

    插件的 `try: from nonebot_plugin_message_reaction import ...` 失败时会静默
    退到替身；没有这个断言，下面所有表态用例都会在「库根本没装上」的情况下
    通过 —— 而生产里同样会静默没有表情，谁也不会发现。
    """
    assert reaction_available is True, "插件退到了表态降级替身，下面的断言测的不是真库的接线"


# ── 顺序：终态表态必须在消息「发出之后」──────────────────────────


async def test_illust_flow_sends_the_push_before_done_reaction(timeline, use_event, monkeypatch):
    """URL hook 成功：🔨 → 发推送 → 🍼。

    装饰器只在函数返回时才打终态，所以发送必须在被装饰的函数内部；
    把发送挪到外面这条断言就变红（见 `_illust_flow` 的 docstring）。
    """
    from nonebot_plugin_pixiv_illust import url_hook

    _patch_client(monkeypatch)
    timeline.patch_send(url_hook, "推送")
    use_event(_group_event())

    await _illust_flow(_group_event(), 29373865)

    assert timeline.events == ["表态:resolving", "发送:推送", "表态:done"]


async def test_illust_flow_sends_failure_text_before_fail_reaction(timeline, use_event, monkeypatch):
    """URL hook 失败：🔨 → 回文案 → ❌。**用户追加要求的那一条**。

    旧实现在这一步是「❌ → 回文案」（异常先穿出被装饰的函数，装饰器已经打过 ❌，
    handler 才去发文案），用户看到的是「先红叉、再出现失败说明」。
    """
    from nonebot_plugin_pixiv_illust import url_hook

    _patch_client(monkeypatch, detail=RuntimeError("pixiv 挂了"))
    timeline.patch_send(url_hook, "失败文案")
    use_event(_group_event())

    with pytest.raises(RuntimeError, match="pixiv 挂了"):
        await _illust_flow(_group_event(), 29373865)

    assert timeline.events == ["表态:resolving", "发送:失败文案", "表态:fail"]


# ── 表态不该扩散到纯取数函数 ──────────────────────────────────────


async def test_pure_fetch_helper_does_not_react(timeline, use_event, monkeypatch):
    """`_build_illust_push` 是纯取数，**不**负责表态。

    表态由调用它的 `_illust_flow` 统一包住 —— 两边都打就会闪两个回合。
    """
    from nonebot_plugin_pixiv_illust import _build_illust_push

    _patch_client(monkeypatch)
    use_event(_group_event())

    msg, rest = await _build_illust_push(29373865)

    assert timeline.events == []
    assert rest == []
    assert "单页插画" in str(msg)


# ── 静态接线：装饰的是哪个函数、闸门有没有排在表态前面 ─────────────


def test_with_reaction_decorates_exactly_the_one_flow():
    """表态只包住「慢操作 + 发送」，不要包住整个 handler。

    包整个 handler 会把「白名单拒绝」「管理员拒绝」「用法提示」这些
    毫秒级的 finish 也裹进表态里 —— 那些路径本不该有任何表情。
    """
    tree = source_introspect.plugin_tree()
    decorated = {
        node.name
        for node in tree.body
        if isinstance(node, source_introspect.ast.AsyncFunctionDef)
        and source_introspect.decorated_with(node, "with_reaction")
    }
    assert decorated == {"_illust_flow"}


def test_no_handler_is_decorated_with_with_reaction():
    """handler 上不该出现 @with_reaction。

    否则「闸门先于表态」的保证就没了：装饰器在 handler 入口就打「处理中」，
    而白名单检查在 handler 内部 —— 被排除的群里也会冒出表情。
    """
    tree = source_introspect.plugin_tree()
    for name, node in source_introspect.handlers(tree).items():
        assert not source_introspect.decorated_with(node, "with_reaction"), (
            f"{name} 被 @with_reaction 包住了，表领会绕过群闸门"
        )


def test_url_hook_group_gate_runs_before_any_reaction():
    """URL hook 里群闸门必须在表态之前。

    否则被排除的群里贴个链接就会冒出 🔨/🍼 表情 —— 卡片不发但表情乱跳，
    比什么都不做更像故障。
    """
    tree = source_introspect.plugin_tree()
    hook = source_introspect.handlers(tree)["url_hook"]
    gate = source_introspect.first_call_lineno(hook, "_is_group_allowed")
    react = source_introspect.first_call_lineno(hook, "_illust_flow")

    assert gate is not None and react is not None
    assert gate < react, "URL hook 的表态排在了群闸门之前"


def test_sender_does_not_finish_the_matcher():
    """`_send_push` 里**不能**有 `finish()`。

    `finish()` 抛 FinishedException 会把控制流直接带走，于是
    ① 终态表态只能写在它之前（顺序又反了）；② 它后面的分流逻辑全成死代码。
    收尾的 finish 归 handler 管。
    """
    tree = source_introspect.plugin_tree()
    fn = next(
        node
        for node in tree.body
        if isinstance(node, source_introspect.ast.AsyncFunctionDef) and node.name == "_send_push"
    )
    called = {
        node.func.attr
        for node in source_introspect.ast.walk(fn)
        if isinstance(node, source_introspect.ast.Call) and isinstance(node.func, source_introspect.ast.Attribute)
    }
    assert "finish" not in called, "_send_push 里出现了 finish()"


def test_terminal_reaction_is_wired_for_the_flow():
    """两条链路（成功 / 失败）都有终态表态的接线（而不是只有 🔨 就收工）。"""
    tree = source_introspect.plugin_tree()
    node = next(
        n for n in tree.body if isinstance(n, source_introspect.ast.AsyncFunctionDef) and n.name == "_illust_flow"
    )
    assert source_introspect.decorated_with(node, "with_reaction"), (
        "_illust_flow 没被 @with_reaction 包住，终态表态不会发出"
    )


async def test_page_download_failure_sends_text_without_claiming_blur(monkeypatch):
    """页图全下不来（403）：文案照发、**不声称已模糊**、也不炸。

    用 `wrap_json` 复刻的真实 JsonDict 形状（缺字段返回 None）喂进去 ——
    「图没下来却写已模糊」是自相矛盾的文案，这条钉住它。
    """
    from nonebot_plugin_pixiv_illust import _build_illust_push, client

    detail = wrap_json(
        {
            "id": 29373865,
            "title": "R18 单页",
            "x_restrict": 1,
            "page_count": 1,
            # App 实测形状：带 CDN 缩放段，poller/hook 负责 strip
            "image_urls": {
                "large": "https://i.pximg.net/c/540x540_70/img-master/img/2026/10/14/12/00/00/111_p0_master1200.jpg"
            },
            "meta_pages": None,
            "user": {"id": 200, "name": "画师甲"},
        }
    )

    async def fake_detail(_illust_id):
        return detail

    async def boom(url, *, blur, radius, max_width=0):
        raise OSError("i.pximg.net 403")

    monkeypatch.setattr(client, "illust_detail", fake_detail)
    monkeypatch.setattr(client, "download_image", boom)

    msg, rest = await _build_illust_push(29373865)

    assert rest == []
    assert "R18 单页" in str(msg)
    assert "已模糊" not in str(msg), "图都没下来，不该声称已模糊"
    assert "未模糊" in str(msg)
