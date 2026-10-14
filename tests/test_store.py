import pytest
from nonebot_plugin_pixiv_illust import store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    """每个测试用独立数据库，绝不碰真实数据。"""
    store.init(tmp_path / "t.sqlite3")
    yield


def test_subscribe_then_list():
    assert (
        store.subscribe(
            group_id=100,
            author_id=200,
            baseline=555,
            author_name="作者甲",
            author_avatar_url="https://i.pximg.net/u1.jpg",
        )
        is True
    )
    rows = store.list_by_group(100)
    assert len(rows) == 1
    assert rows[0]["author_id"] == 200
    assert rows[0]["last_seen"] == 555
    assert rows[0]["author_name"] == "作者甲"
    assert rows[0]["author_avatar_url"] == "https://i.pximg.net/u1.jpg"
    assert rows[0]["created_at"] > 0  # 订阅时间（unix 秒）


def test_subscribe_without_author_info_still_works():
    """作者名/头像取不到时（API 挂了）也要能订阅成功。"""
    assert store.subscribe(100, 200, baseline=0) is True
    row = store.list_by_group(100)[0]
    assert row["author_name"] == ""
    assert row["author_avatar_url"] == ""


def test_subscribe_is_idempotent_and_keeps_high_watermark():
    store.subscribe(100, 200, baseline=555, author_name="旧名")
    assert store.subscribe(100, 200, baseline=999, author_name="新名") is False  # 重复订阅返回 False
    assert store.list_by_group(100)[0]["last_seen"] == 555  # 高水位不被拉回
    assert store.list_by_group(100)[0]["author_name"] == "旧名"  # 也不覆盖已有信息


def test_unsubscribe():
    store.subscribe(100, 200, baseline=0)
    assert store.unsubscribe(100, 200) is True
    assert store.list_by_group(100) == []
    assert store.unsubscribe(100, 200) is False  # 再退订返回 False


def test_list_by_author_across_groups():
    store.subscribe(100, 200, baseline=1)
    store.subscribe(300, 200, baseline=2)
    assert [r["group_id"] for r in store.list_by_author(200)] == [100, 300]


def test_set_last_seen_only_moves_forward():
    store.subscribe(100, 200, baseline=100)
    store.set_last_seen(100, 200, 500)
    assert store.list_by_group(100)[0]["last_seen"] == 500
    store.set_last_seen(100, 200, 300)  # 回退值应被忽略
    assert store.list_by_group(100)[0]["last_seen"] == 500


def test_all_authors_distinct():
    store.subscribe(100, 200, baseline=0)
    store.subscribe(300, 200, baseline=0)
    store.subscribe(100, 400, baseline=0)
    assert sorted(store.all_authors()) == [200, 400]


def test_update_author_info_fills_in_missing_data():
    """老订阅（无作者名）可以通过 update_author_info 补上。"""
    store.subscribe(100, 200, baseline=0)
    store.update_author_info(200, name="补上的名字", avatar_url="https://i.pximg.net/a.jpg")
    row = store.list_by_group(100)[0]
    assert row["author_name"] == "补上的名字"
    assert row["author_avatar_url"] == "https://i.pximg.net/a.jpg"


def test_update_author_info_does_not_overwrite_with_empty():
    store.subscribe(100, 200, baseline=0, author_name="原名")
    store.update_author_info(200, name="", avatar_url="")
    assert store.list_by_group(100)[0]["author_name"] == "原名"


def test_migration_from_old_schema(tmp_path):
    """老库（没有 author_name 列）打开时应自动补列，不丢数据。"""
    import sqlite3

    old = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(old)
    conn.executescript(
        "CREATE TABLE subscription ("
        " group_id INTEGER NOT NULL, author_id INTEGER NOT NULL,"
        " last_seen INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,"
        " PRIMARY KEY (group_id, author_id));"
    )
    conn.execute("INSERT INTO subscription VALUES (1, 2, 3, 4)")
    conn.commit()
    conn.close()

    store.init(old)
    row = store.list_by_group(1)[0]
    assert row["author_id"] == 2
    assert row["last_seen"] == 3
    assert row["author_name"] == ""  # 新列有默认值
