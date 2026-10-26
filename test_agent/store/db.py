"""第 5 层数据层：SQLite 连接与建库。

约定（与 docs/layer5-data-schema.sql 头部一致）：
- WAL 模式；外键运行时开启，防绕过确认门的脏写入
- 方言 SQLite，仅用标准库 sqlite3，无第三方依赖
"""
import sqlite3
from pathlib import Path

from .. import config


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """打开连接：WAL + busy_timeout 防并发读写撞锁；行取回用 Row 便于按列名读。"""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# 轻量列迁移：老库缺列则 ALTER（新库由 schema 直接建出）。加列时同步改 schema 文件。
_MIGRATIONS = {
    "test_tasks": {"coverage_target": "REAL"},   # A1：准出标准在分析阶段冻结进任务
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if have and col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


def init_db(db_path: Path | None = None) -> int:
    """按 schema 建库（幂等：已有表则跳过）+ 列迁移。返回业务表数量供自检。"""
    conn = get_conn(db_path)
    try:
        n_tables = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchone()["n"]
        if n_tables == 0:
            conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        _migrate(conn)
        return conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchone()["n"]
    finally:
        conn.close()
