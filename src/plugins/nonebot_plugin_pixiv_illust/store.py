"""订阅持久化。

用标准库 sqlite3，不引入 ORM / alembic —— 只有一张表，加迁移是过度设计。
数据库位置由 `init()` 决定；生产用 localstore 的插件数据目录，测试传 tmp_path。

表结构（v2，多了 author_name / author_avatar_url）：
    subscription(group_id, author_id, last_seen, created_at, author_name, author_avatar_url)

`init()` 会做**加列式**轻量迁移（ALTER TABLE ADD COLUMN），老库能直接升上来。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

_TABLE = "subscription"

# 建表语句（全新库）
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    group_id          INTEGER NOT NULL,
    author_id         INTEGER NOT NULL,
    last_seen         INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    author_name       TEXT    NOT NULL DEFAULT '',
    author_avatar_url TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, author_id)
);
"""

# 增量列（老库补齐用）。列名 → 列定义
_ADDED_COLUMNS: dict[str, str] = {
    "author_name": "TEXT NOT NULL DEFAULT ''",
    "author_avatar_url": "TEXT NOT NULL DEFAULT ''",
}

_db_path: Path | None = None


def default_db_path() -> Path:
    """生产路径：localstore 的插件数据目录。只在启动时调用（依赖 NoneBot 已初始化）。"""
    import nonebot_plugin_localstore as localstore

    return localstore.get_plugin_data_file("pixiv_illust.db")


def init(path: Path) -> None:
    """指定数据库文件、建表、补列。启动钩子/测试都走这里。"""
    global _db_path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = path
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """把老库缺的列补上（幂等）。"""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()}
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {column} {ddl}")


@contextlib.contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise RuntimeError("store.init() 还没调用")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def subscribe(
    group_id: int,
    author_id: int,
    baseline: int = 0,
    *,
    author_name: str = "",
    author_avatar_url: str = "",
) -> bool:
    """新增订阅，True=新建；已存在返回 False 且**不覆盖**已有信息。"""
    with _conn() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {_TABLE}"
            " (group_id, author_id, last_seen, created_at, author_name, author_avatar_url)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, author_id, baseline, int(time.time()), author_name, author_avatar_url),
        )
        return cur.rowcount > 0


def unsubscribe(group_id: int, author_id: int) -> bool:
    """退订，True=确实删掉了。"""
    with _conn() as conn:
        cur = conn.execute(
            f"DELETE FROM {_TABLE} WHERE group_id = ? AND author_id = ?",
            (group_id, author_id),
        )
        return cur.rowcount > 0


def list_by_group(group_id: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT author_id, author_name, author_avatar_url, last_seen, created_at"
            f" FROM {_TABLE} WHERE group_id = ? ORDER BY created_at",
            (group_id,),
        ).fetchall()


def list_by_author(author_id: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            f"SELECT group_id, last_seen FROM {_TABLE} WHERE author_id = ? ORDER BY group_id",
            (author_id,),
        ).fetchall()


def all_authors() -> list[int]:
    """所有被订阅的作者 ID（去重）—— 轮询时按作者查一次 API。"""
    with _conn() as conn:
        rows = conn.execute(f"SELECT DISTINCT author_id FROM {_TABLE}").fetchall()
    return [r["author_id"] for r in rows]


def set_last_seen(group_id: int, author_id: int, illust_id: int) -> None:
    """推进高水位，只允许前进（`last_seen < ?` 条件保证）。"""
    with _conn() as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET last_seen = ? WHERE group_id = ? AND author_id = ? AND last_seen < ?",
            (illust_id, group_id, author_id, illust_id),
        )


def update_author_info(author_id: int, *, name: str = "", avatar_url: str = "") -> None:
    """补齐/刷新作者信息（同一次订阅可能跨多个群，所以按 author_id 全表更新）。

    **空值不覆盖**：取不到名字时不要把已有的好数据抹掉。
    """
    with _conn() as conn:
        if name:
            conn.execute(f"UPDATE {_TABLE} SET author_name = ? WHERE author_id = ?", (name, author_id))
        if avatar_url:
            conn.execute(
                f"UPDATE {_TABLE} SET author_avatar_url = ? WHERE author_id = ?",
                (avatar_url, author_id),
            )
