"""handlers 的单测：ID 提取、引用 id、回复文案、画师标签降级。

**本仓与参照仓（pixiv 小说）的差异**：插画没有「全文」概念，图由 URL hook / 轮询
直接发出，所以 `deliver_novel_text` 三级降级与 `safe_filename` 文件名清洗**整体不存在**
（连用例一起删）—— 取而代之的是一条钉住「这些 API 不该出现在本仓」的用例。

文案口径也从「作者」改成「画师」（`author_id` / `author_name` 是 store 的列名，保持不变）。
"""

from types import SimpleNamespace

import pytest
from nonebot_plugin_pixiv_illust import handlers, store
from nonebot_plugin_pixiv_illust.handlers import (
    extract_id,
    reply_list,
    reply_message_id,
    reply_subscribe,
    reply_unsubscribe,
)


@pytest.fixture(autouse=True)
def _db(tmp_path):
    store.init(tmp_path / "t.sqlite3")
    yield


# ── ID 提取（参数既可以是纯数字，也可以直接贴链接）──────────────────


def test_extract_id_takes_plain_number():
    assert extract_id("12345") == 12345
    assert extract_id("  12345  ") == 12345


def test_extract_id_takes_id_from_url():
    assert extract_id("https://www.pixiv.net/artworks/12345") == 12345
    assert extract_id("https://www.pixiv.net/users/12345") == 12345
    assert extract_id("https://www.pixiv.net/i/666") == 666
    assert extract_id("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=67890") == 67890


def test_extract_id_rejects_garbage():
    assert extract_id("abc") is None
    assert extract_id("") is None
    assert extract_id(None) is None


def test_extract_id_needs_at_least_three_digits():
    """两位数字不算 ID —— 防手滑把「第 12 张」之类的碎数字当成画师/作品 id。"""
    assert extract_id("12") is None


# ── 订阅 / 退订 / 列表的回复文案 ────────────────────────────────


def test_reply_subscribe_creates_and_reports():
    text = reply_subscribe(group_id=100, author_id=200, baseline=555, author_name="画师甲")
    assert "订阅" in text
    assert "画师甲" in text  # 画师名要显示出来
    assert store.list_by_group(100)[0]["author_id"] == 200


def test_reply_subscribe_stores_author_name_and_avatar():
    reply_subscribe(
        group_id=100,
        author_id=200,
        baseline=555,
        author_name="画师甲",
        author_avatar_url="https://i.pximg.net/avatar.jpg",
    )
    row = store.list_by_group(100)[0]
    assert row["author_name"] == "画师甲"
    assert row["author_avatar_url"] == "https://i.pximg.net/avatar.jpg"


def test_reply_subscribe_works_without_author_name():
    """取不到画师信息时也要能订阅成功（只是显示降级成 ID）。"""
    text = reply_subscribe(100, 200, baseline=555)
    assert "订阅" in text
    assert "200" in text
    assert store.list_by_group(100)[0]["author_name"] == ""


def test_reply_subscribe_twice_says_already():
    reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    text = reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    assert "已经" in text or "重复" in text


def test_reply_unsubscribe_ok_and_not_found():
    reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    assert "退订" in reply_unsubscribe(100, 200)
    assert "没有" in reply_unsubscribe(100, 200)


def test_reply_list_prefers_author_name_and_falls_back_to_id():
    """回退用的纯文本列表：有名字显示名字，没名字显示 ID。"""
    reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    reply_subscribe(100, 300, baseline=1, author_name="")
    text = reply_list(100)
    assert "画师甲" in text
    assert "300" in text
    assert "1." in text and "2." in text


def test_reply_list_empty():
    assert "没有" in reply_list(100)


# ── 文案口径：说「画师」，不说「作者」（订阅的是画师的新作）──────────


def test_reply_texts_say_artist_not_author():
    """本插件的文案口径是「画师」；漏改一处就会在群里露出小说插件的措辞。"""
    from nonebot_plugin_pixiv_illust.message import user_url

    created = reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    assert "画师甲" in created and "作者" not in created
    assert user_url(200) in created

    again = reply_subscribe(100, 200, baseline=1, author_name="画师甲")
    assert "作者" not in again

    assert "作者" not in reply_unsubscribe(100, 200)
    assert "作者" not in reply_unsubscribe(100, 200)
    assert "作者" not in reply_list(100)
    assert "作者" not in reply_list(999)


def test_reply_subscribe_warn_points_at_illust_commands():
    """警告里给的补救命令必须是**本插件**的「退订插画」，不能抄成小说的「退订」。"""
    text = reply_subscribe(group_id=100, author_id=200, baseline=0, author_fetch_failed=True)
    assert "退订插画" in text


# ── 画师信息拉取失败必须警告（否则 ID 打错 → 「✅ 已订阅」→ 永远静默）──


def test_reply_subscribe_warns_when_author_fetch_failed():
    """有 token 但拉不到画师信息（多半是 ID 打错）→ 回复里必须有警告。

    没有这条警告，用户看到「✅ 已订阅」却永远收不到推送，还找不到原因。
    """
    text = reply_subscribe(group_id=100, author_id=99999999999, baseline=0, author_fetch_failed=True)
    assert "已订阅" in text  # 订阅仍然生效（不因拉取失败而拒绝）
    assert "⚠️" in text  # 但必须带警告
    assert "ID" in text  # 且点明是 ID 可能有问题
    assert store.list_by_group(100)[0]["author_id"] == 99999999999


def test_reply_subscribe_silent_when_fetch_ok():
    """正常路径**不能**出现警告 —— 否则每次订阅都吓唬用户一次。"""
    text = reply_subscribe(group_id=100, author_id=200, baseline=555, author_name="画师甲")
    assert "⚠️" not in text


def test_reply_subscribe_no_warning_when_already_subscribed():
    """重复订阅走「已经订阅过」分支，此时**不该**再叠加 ID 错误警告。

    那个警告是给「第一次订阅且拉不到画师」用的；重复订阅时画师早已在库里，
    再报一次会误导用户以为订阅有毛病。
    """
    reply_subscribe(group_id=100, author_id=200, baseline=555, author_name="画师甲")
    again = reply_subscribe(group_id=100, author_id=200, baseline=555, author_fetch_failed=True)
    assert "已经订阅过" in again
    assert "⚠️" not in again


def test_reply_list_falls_back_to_id_for_sqlite_row():
    """画师名缺失时显示 ID —— 不能把 `sqlite3.Row` 直接 str 进回复里。

    只有 `isinstance(row, dict)` 判断会让 `sqlite3.Row` 掉到 `str(row)`，
    用户在群里就会看到 `<sqlite3.Row object at 0x...>`。
    """
    store.subscribe(100, 200, baseline=1, author_name="")
    text = reply_list(100)
    assert "sqlite3.Row" not in text
    assert "200" in text


# ── 读引用 id（两种实现形状都要支持；URL hook 靠它引用带链接的那条消息）──


def test_reply_id_from_pydantic_reply():
    """形状 A：适配器把 `event.reply` 填成 pydantic 模型（字段 `message_id`）。"""
    event = SimpleNamespace(reply=SimpleNamespace(message_id=1001))
    assert reply_message_id(event) == 1001


def test_reply_id_from_reply_segment():
    """形状 B：实现把引用塞成一个 `type="reply"` 的 MessageSegment，id 在 `data["id"]`。

    这是 LLBot 的实际形状（实测其源码用 `{type:"reply", data:{id: shortId}}`）。
    只支持形状 A 的写法在这里会静默返回 None。
    """
    seg = SimpleNamespace(type="reply", data={"id": "1001"})
    event = SimpleNamespace(reply=seg)
    assert reply_message_id(event) == 1001


def test_reply_id_from_message_body_when_reply_attr_is_empty():
    """`event.reply` 是空的，但消息体里有 reply 段 —— 也要能取到。"""
    seg = SimpleNamespace(type="reply", data={"id": "1001"})
    event = SimpleNamespace(reply=None, get_message=lambda: [seg])
    assert reply_message_id(event) == 1001


def test_no_reply_returns_none():
    event = SimpleNamespace(reply=None, get_message=lambda: [])
    assert reply_message_id(event) is None


def test_event_without_reply_attribute_returns_none():
    """连 `reply` 属性都没有的事件（某些实现/事件类型）不能抛 AttributeError。"""
    assert reply_message_id(SimpleNamespace()) is None


def test_reply_id_zero_is_treated_as_no_reply():
    """`id=0` 是实现「找不到原消息」时的占位值。

    当成真 id 用会让下游以为「这条消息引用成功了」，实际根本没引用上。
    """
    event = SimpleNamespace(reply=SimpleNamespace(message_id=0))
    assert reply_message_id(event) is None


def test_reply_id_garbage_is_ignored():
    event = SimpleNamespace(reply=SimpleNamespace(message_id="not-a-number"))
    assert reply_message_id(event) is None


# ── 本仓刻意没有的 API（全文投递 / txt 文件名清洗）─────────────────


def test_no_fulltext_delivery_api():
    """插画没有「全文」，所以参照仓的全文投递与文件名清洗**不该**被搬过来。

    只要这两个名字还在，就说明有人把小说的「获取全文」链路顺手复制了进来 ——
    那条链路依赖 store 的 card_message 表与本仓不存在的配置项，留着就是死代码。
    """
    assert not hasattr(handlers, "deliver_novel_text")
    assert not hasattr(handlers, "deliver_illust_text")
    assert not hasattr(handlers, "safe_filename")
