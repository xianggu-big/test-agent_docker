"""第 5 层数据层：SQLite 连接与建库。

约定（与 docs/layer5-data-schema.sql 头部一致）：
- WAL 模式；外键运行时开启，防绕过确认门的脏写入
- 方言 SQLite，仅用标准库 sqlite3，无第三方依赖
"""
import inspect
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

# —— 连接探针（2026-11-08 655/656 锁循环诊断设施，MTA_CONN_PROBE 默认关）——
# 病根家族：未提交事务悬挂持锁（api 端点实锤过 19/20 locked 雪崩，write_conn 为其
# 而生；执行图/钩子路径复发=655/656 worker 卡死在 UPDATE 上数分钟不自愈）。
# 探针=注册每个 get_conn 产物（id→(连接, 创建栈摘要)）+后台线程周期点名 in_transaction
# 的连接——悬挂持锁者从"只能猜"变"日志直接报创建位置"。
# 注意：sqlite3.Connection 不支持 weakref（TypeError 实锤）——强引用注册，探针轮询时
# 注销已 close 的连接（close 后 OS 层资源已释放，Python 对象延迟数秒析构无锁影响）。
_CONN_REG: dict = {}             # id(conn) -> (conn, 创建位置摘要)
_REG_LOCK = threading.Lock()
_PROBE_STARTED = False


def _register_conn(conn: sqlite3.Connection) -> None:
    global _PROBE_STARTED
    origin = "?"
    frame = inspect.currentframe()
    try:
        # _register_conn ← get_conn ← 真正的调用方（再上一层是调用方的调用方，更有用）
        f = frame.f_back.f_back if frame and frame.f_back else None
        if f is not None:
            origin = f"{f.f_code.co_filename.rsplit(chr(92), 1)[-1]}:{f.f_lineno} in {f.f_code.co_name}"
    finally:
        del frame
    with _REG_LOCK:
        _CONN_REG[id(conn)] = (conn, origin)
        if not _PROBE_STARTED:
            _PROBE_STARTED = True
            threading.Thread(target=_probe_loop, name="conn-probe", daemon=True).start()


def _probe_loop() -> None:
    """每 5s 扫描：点名悬挂连接；同 (线程,创建位置) 累计 2 个周期（≈10s）仍悬挂→
    自动 rollback 止血（未提交事务悬挂持锁的运行时兜底）。
    计数按 (thread_ident, origin) 而非连接 id——661 实锤：重试路径每轮新建连接，
    id 轮换导致按 id 计数永远=1 止血永不触发。正常流程写→commit 是毫秒级，探针
    5s 采样撞见概率≈0，连续两周期命中=必然异常悬挂。止血语义：回滚=丢弃半个事务，
    被回滚方（重试路径）下轮重写（与 write_conn 异常 rollback 同语义）。"""
    stuck_cnt: dict = {}
    while True:
        time.sleep(5)
        try:
            stuck, closed, healed = [], [], []
            with _REG_LOCK:
                for cid, (c, origin) in list(_CONN_REG.items()):
                    key = (threading.get_ident(), origin)
                    try:
                        if c.in_transaction:
                            cnt = stuck_cnt.get(key, 0) + 1
                            stuck_cnt[key] = cnt
                            stuck.append(origin)
                            if cnt >= 4:
                                # 20s（4 周期）才动手：活跃写（写→commit 毫秒级）绝不连挂
                                # 4 周期；669 实锤 2 周期止血会回滚 D2 增补的活跃写=数据丢
                                try:
                                    c.rollback()
                                    healed.append(origin)
                                except sqlite3.ProgrammingError:
                                    closed.append(cid)
                                stuck_cnt.pop(key, None)   # 止血后重开计数（连接保留监控）
                        else:
                            stuck_cnt.pop(key, None)
                    except sqlite3.ProgrammingError:
                        closed.append(cid)
                        stuck_cnt.pop((threading.get_ident(), origin), None)
                for cid in closed:
                    _CONN_REG.pop(cid, None)
            if stuck:
                log.warning("[conn-probe] %d 个连接处于未提交事务（悬挂持锁）：%s",
                            len(stuck), stuck[:8])
            if healed:
                log.warning("[conn-probe] 自动止血 rollback %d 个悬挂连接：%s",
                            len(healed), healed[:8])
        except Exception:        # noqa: BLE001 —— 探针自身永不炸业务
            pass


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """打开连接：WAL + busy_timeout 防并发读写撞锁；行取回用 Row 便于按列名读。
    check_same_thread=False：允许跨线程访问——项目模式是"谁建谁用"短连接（无并发
    共用），此开关只为 conn-probe 止血能力（2026-11-08 661 实锤：探针线程对主线程
    创建的悬挂连接读 in_transaction 可行、rollback 被 check_same_thread 拦截抛
    ProgrammingError→止血永远不生效）。"""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")   # 并发写雪崩实锤后上调（安全审计 20 并发创建 19 locked）
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    if getattr(config, "CONN_PROBE_ENABLED", False):
        _register_conn(conn)
    return conn


# 轻量列迁移：老库缺列则 ALTER（新库由 schema 直接建出）。加列时同步改 schema 文件。
_MIGRATIONS = {
    "projects": {"repo_key": "TEXT"},     # B2 项目归一：老库补键列（backfill 脚本写实+合并重复）
    "test_tasks": {"coverage_target": "REAL",    # A1：准出标准在分析阶段冻结进任务
                   "auto_prepare": "INTEGER NOT NULL DEFAULT 0",  # 沙箱自适应：任务级「自动准备环境」开关
                   "two_stage": "INTEGER NOT NULL DEFAULT 0",     # 两阶段人机协作：确认=启动阶段一，停在 ready_for_review
                   "ai_reviewer": "TEXT NOT NULL DEFAULT 'off'",  # AI 代审三模式 off/hybrid/auto
                   "verify_mode": "INTEGER NOT NULL DEFAULT 0"},  # A2 验证任务冻结：用例集全程只读（图级拦截生成/剪除/重写）
    "test_reports": {"per_file": "TEXT",         # B2：逐文件覆盖明细（缺口回灌+趋势图数据源）
                     "narrative": "TEXT",        # gen_report 叙述正文（E5 审查对象）
                     "review": "TEXT",           # 报告二次审查结论 JSON（§3.4 双闸）
                     "analysis": "TEXT"},        # AI 缺陷分析与修复建议 JSON（报告概览失败框，按需生成缓存）
    "test_cases": {"priority": "TEXT", "input": "TEXT", "steps": "TEXT",
                   "expected": "TEXT", "expected_basis": "TEXT"},  # 六要素结构化（§3.3）
    "evaluation_rounds": {"level": "TEXT DEFAULT 'L1'"},  # 四级双轨：老库补级别维度，存量行=L1
    "call_edges": {"file": "TEXT", "lineno": "INTEGER"},  # 跨级溯源：老库补函数定义位置
    "test_scripts": {"filename": "TEXT"},        # 两阶段：布考场按审查结论重建生成文件的键
    "llm_calls": {"truncated": "INTEGER NOT NULL DEFAULT 0"},  # 任务卡-H：上下文截断对账闸打标
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
    _rebuild_table_check(conn, "test_tasks", "test_tasks")


def _endpoints_source_check_outdated(conn: sqlite3.Connection) -> bool:
    """老库 interface_endpoints 的 CHECK 枚举缺 go_route/go_swag（2026-11-11/12 接入
    Go 提取来源时漏扩——落库 IntegrityError→Go L2 诚实降级 no_spec，矩阵最后一格被卡）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='interface_endpoints'").fetchone()
    return bool(row) and "go_route" not in (row["sql"] or "")


def _rebuild_table_check(conn: sqlite3.Connection, table: str, tmp_prefix: str) -> None:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\n\);",
                  config.SCHEMA_PATH.read_text(encoding="utf-8"), re.S)
    if not m:
        raise RuntimeError(f"schema 文件中未找到 {table} DDL，无法重建")
    tmp = f"{tmp_prefix}__mig"
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    conn.executescript(f"DROP TABLE IF EXISTS {tmp};")
    conn.executescript(m.group(0).replace(f"IF NOT EXISTS {table}",
                                          f"IF NOT EXISTS {tmp}", 1) + ";")
    old_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    new_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({tmp})")]
    common = [c for c in new_cols if c in old_cols]
    collist = ", ".join(common)
    conn.execute(f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    # 幂等执行整个 schema（IF NOT EXISTS）：老库自动补新表与索引，DDL 单一真源不漂移
    conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    # CHECK 枚举无法 ALTER：老库 test_tasks 缺 ready_for_review 时按 12 步法重建（两阶段停等态）
    if _tasks_status_check_outdated(conn):
        _rebuild_test_tasks(conn)
    if _endpoints_source_check_outdated(conn):
        _rebuild_table_check(conn, "interface_endpoints", "interface_endpoints")
    # 重建（DROP TABLE）会连带删掉该表全部索引——同轮开头的 executescript 已跑过
    # 不会补，老库首启触发重建的整个服务会话无索引。收尾再幂等执行一遍 schema
    # （IF NOT EXISTS 只补缺失索引，不动表数据——2026-11-15 闲时找bug :memory: 实锤）
    conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if have and col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _backfill_project_names(conn)   # 幂等：路径形态项目名→末段（用户反馈 2026-11-03）——必须在 commit 前
    _backfill_verify_mode(conn)     # A2（2026-11-20）：历史验证任务（requirement 软链前缀）回填冻结标记
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
    finally:
        conn.close()
    if db_path is None:              # 仅真实库：恢复上次保存的 LLM profile（测试 tmp 库不动全局态）
        try:
            from ..skills.llm_profile import apply_saved_if_any   # 函数内延迟导入防循环（坑预判 #1）
            apply_saved_if_any()
        except Exception as e:       # noqa: BLE001 —— 恢复失败不挡启动
            log.warning("llm profile 启动恢复失败: %s: %s", type(e).__name__, e)
    return _count_tables(db_path)


def _count_tables(db_path: Path | None = None) -> int:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name != 'sqlite_sequence'"
        ).fetchone()["n"]
    finally:
        conn.close()


from contextlib import contextmanager


def _backfill_project_names(conn) -> None:
    """存量项目名修复（用户反馈：本地路径做项目名太长）：name 为路径形态
    （含 / 或反斜杠）的一次性更新为末段；幂等（已是末段的不命中）。"""
    bs = chr(92)                     # 反斜杠（规避转义坑：不用正则，纯字符串分段）
    for r in conn.execute("SELECT id, name, repo_url FROM projects").fetchall():
        name = r["name"] or ""
        if "/" not in name and bs not in name:
            continue
        src = (r["repo_url"] or name).strip()
        while src and src[-1] in ("/", bs):
            src = src[:-1]
        seg = [x for x in src.replace(bs, "/").split("/") if x]
        new_name = seg[-1] if seg else name
        if new_name and new_name != name:
            conn.execute("UPDATE projects SET name=? WHERE id=?", (new_name, r["id"]))


def _backfill_verify_mode(conn) -> None:
    """A2（2026-11-20）：历史验证任务回填冻结标记——create_verify_task 以 requirement
    软链前缀「[回归验证 of task N]」为唯一标识（列迁移前的历史任务 verify_mode=0），
    执行图冻结拦截依赖该列。只补 0 值行（幂等）；普通任务零命中零改动。"""
    conn.execute("UPDATE test_tasks SET verify_mode=1 "
                 "WHERE verify_mode=0 AND requirement_text LIKE '[回归验证 of task %'")


@contextmanager
def write_conn(db_path: Path | None = None):
    """写端点专用连接（安全审计实锤：api_create_task 等写端点不 close，
    500 时未提交事务悬挂持锁 → 并发创建 19/20 database is locked 雪崩）。
    用法：with write_conn() as conn: ...（异常自动 rollback+close）。"""
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_agent_message(conn, session_id: int, role: str, content: str) -> None:
    """会话消息原子追加（B2，2026-11-27）：seq 取号内联在 INSERT 子查询里——
    `(SELECT COALESCE(MAX(seq),-1)+1 FROM agent_messages WHERE session_id=?)`，
    SQLite 单写锁下同语句取号+写入原子化。旧口径先 COUNT(*) 再 INSERT 有两类坏法：
    ①两连接并发 COUNT 同值 → UNIQUE(session_id,seq) IntegrityError；
    ②删过消息后 COUNT 偏小 → 序号重用同撞 UNIQUE。"""
    conn.execute(
        "INSERT INTO agent_messages(session_id, seq, role, content) "
        "VALUES (?, (SELECT COALESCE(MAX(seq),-1)+1 FROM agent_messages "
        "WHERE session_id=?), ?, ?)",
        (session_id, session_id, role, content))
    conn.commit()
