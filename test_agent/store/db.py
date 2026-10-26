"""第 5 层数据层：SQLite 连接与建库。

约定（与 docs/layer5-data-schema.sql 头部一致）：
- WAL 模式；外键运行时开启，防绕过确认门的脏写入
- 方言 SQLite，仅用标准库 sqlite3，无第三方依赖
"""
import sqlite3
from pathlib import Path

from .. import config


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """打开连接：行取回用 Row 便于按列名读。"""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> int:
    """按 schema 建库（幂等：已有表则跳过）。返回业务表数量供自检。"""
    conn = get_conn(db_path)
    try:
        n_tables = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchone()["n"]
        if n_tables == 0:
            conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        return conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchone()["n"]
    finally:
        conn.close()
