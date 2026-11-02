"""第 5 层数据层：SQLite 连接与建库。

约定（与 docs/layer5-data-schema.sql 头部一致）：
- WAL 模式；外键运行时开启，防绕过确认门的脏写入
- 方言 SQLite，仅用标准库 sqlite3，无第三方依赖
"""
import re
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
    "projects": {"repo_key": "TEXT"},     # B2 项目归一：老库补键列（backfill 脚本写实+合并重复）
    "test_tasks": {"coverage_target": "REAL",    # A1：准出标准在分析阶段冻结进任务
                   "auto_prepare": "INTEGER NOT NULL DEFAULT 0",  # 沙箱自适应：任务级「自动准备环境」开关
                   "two_stage": "INTEGER NOT NULL DEFAULT 0",     # 两阶段人机协作：确认=启动阶段一，停在 ready_for_review
                   "ai_reviewer": "TEXT NOT NULL DEFAULT 'off'"},  # AI 代审三模式 off/hybrid/auto
    "test_reports": {"per_file": "TEXT",         # B2：逐文件覆盖明细（缺口回灌+趋势图数据源）
                     "narrative": "TEXT",        # gen_report 叙述正文（E5 审查对象）
                     "review": "TEXT",           # 报告二次审查结论 JSON（§3.4 双闸）
                     "analysis": "TEXT"},        # AI 缺陷分析与修复建议 JSON（报告概览失败框，按需生成缓存）
    "test_cases": {"priority": "TEXT", "input": "TEXT", "steps": "TEXT",
                   "expected": "TEXT", "expected_basis": "TEXT"},  # 六要素结构化（§3.3）
    "evaluation_rounds": {"level": "TEXT DEFAULT 'L1'"},  # 四级双轨：老库补级别维度，存量行=L1
    "call_edges": {"file": "TEXT", "lineno": "INTEGER"},  # 跨级溯源：老库补函数定义位置
    "test_scripts": {"filename": "TEXT"},        # 两阶段：布考场按审查结论重建生成文件的键
}


def _tasks_status_check_outdated(conn: sqlite3.Connection) -> bool:
    """老库 test_tasks 的 CHECK 枚举缺 ready_for_review（两阶段停等态）时需要重建表。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='test_tasks'").fetchone()
    return bool(row) and "ready_for_review" not in (row["sql"] or "")


def _rebuild_test_tasks(conn: sqlite3.Connection) -> None:
    """SQLite 的 CHECK 约束无法 ALTER——加状态枚举值按官方 12 步法重建表。

    新表 DDL 从 schema 文件解析（单一真源不漂移）；列按新老交集拷贝（老库经
    历过 ALTER 补列，列序可能与新 DDL 不一致，SELECT * 按位拷贝会错位）；
    foreign_keys 临时关闭（其他表 REFERENCES test_tasks 按名解析，rename 后自动指回新表）。
    """
    m = re.search(r"CREATE TABLE IF NOT EXISTS test_tasks\s*\(.*?\n\);",
                  config.SCHEMA_PATH.read_text(encoding="utf-8"), re.S)
    if not m:
        raise RuntimeError("schema 文件中未找到 test_tasks DDL，无法重建")
    tmp = "test_tasks__mig"
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    conn.executescript(f"DROP TABLE IF EXISTS {tmp};")
    conn.executescript(m.group(0).replace("IF NOT EXISTS test_tasks",
                                          f"IF NOT EXISTS {tmp}", 1) + ";")
    old_cols = [r["name"] for r in conn.execute("PRAGMA table_info(test_tasks)")]
    new_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({tmp})")]
    common = [c for c in new_cols if c in old_cols]
    collist = ", ".join(common)
    conn.execute(f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM test_tasks")
    conn.execute("DROP TABLE test_tasks")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO test_tasks")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    # 幂等执行整个 schema（IF NOT EXISTS）：老库自动补新表与索引，DDL 单一真源不漂移
    conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    # CHECK 枚举无法 ALTER：老库 test_tasks 缺 ready_for_review 时按 12 步法重建（两阶段停等态）
    if _tasks_status_check_outdated(conn):
        _rebuild_test_tasks(conn)
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
