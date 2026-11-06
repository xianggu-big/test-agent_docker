"""db_assert：DB 验证 skill（黑盒能力补齐阶段2，docs/黑盒能力补齐任务包-20261106.md）。

五步模子对应：②提取层=sidecar 真实建库（db_sidecar）；③生成层=LLM 只生成验证 SQL、
**只读闸**（sqlglot 解析 AST，白名单仅 SELECT/SHOW；任一语句是 UPDATE/DELETE/DROP/
INSERT/ALTER 等非白名单类型，或语法不可解析 → **整案剔除**——一条都不执行）并留痕；
④执行层=sidecar 容器内执行（mariadb -e / psql -c，确定性）；⑤落库层=db_asserts 第 29 表
（SQL 原文/verdict/行数/错误/拒绝理由，幂等口径与 app_logs 一致）。

安全模型：只读闸在 skill 内部（不占确认门——闸挡在执行之前，与 import 白名单同哲学）；
注入形态 `SELECT 1; DROP TABLE x` 由多语句拆分逐条判定，任一违规整案拒绝。
"""
import json
import logging
import time
from pathlib import Path

from .. import config
from ..store.db import get_conn

log = logging.getLogger(__name__)

# sqlglot 语句白名单（AST 根节点类型）：查询家族。SHOW 保留任务卡明文；
# UNION/EXCEPT/INTERSECT/CTE 是 SELECT 的复合形态，根节点类型不同但语义只读。
_STMT_OK_NAMES = ("Select", "Show", "Union", "Except", "Intersect", "Subquery", "CTE")


def _readonly_gate(sql_text: str) -> tuple[list[str], list[str]]:
    """只读闸：返回 (放行语句列表, 拒绝留痕列表)。

    规则（任务卡明文）：白名单仅 SELECT/SHOW；出现 UPDATE/DELETE/DROP 整案剔除留痕。
    "整案"语义从严：多语句中任一条违规 → 整案拒绝（kept=[]），拒绝理由指明语句类型。
    """
    import sqlglot
    from sqlglot.errors import ParseError

    raw = (sql_text or "").strip().rstrip(";")
    if not raw:
        return [], ["空 SQL：无语句可验证"]
    try:
        statements = sqlglot.parse(raw)
    except ParseError as e:
        return [], [f"语法不可解析（拒绝执行）：{str(e)[:200]}"]
    if not statements:
        return [], ["空 SQL：解析结果为空"]
    kept: list[str] = []
    rejected: list[str] = []
    for stmt in statements:
        if stmt is None:
            rejected.append("空语句（; 连续分号）不在白名单")
            continue
        kind = type(stmt).__name__
        if kind == "Command":
            # sqlglot 对 SHOW 等方言语句给 Command（无完整 AST）——按首词前缀判定：
            # 仅 SHOW 放行（白名单明文），PRAGMA/SET/EXPLAIN ANALYZE 等 Command 一律拒绝
            head = (stmt.sql() or "").strip().split(None, 1)
            if head and head[0].upper() == "SHOW":
                kept.append(stmt.sql())
            else:
                rejected.append(f"语句类型 {kind}({(head[0] if head else '?').upper()}) "
                                f"不在只读白名单（SELECT/SHOW）——整案剔除")
        elif kind in _STMT_OK_NAMES:
            kept.append(stmt.sql())
        else:
            rejected.append(f"语句类型 {kind} 不在只读白名单（SELECT/SHOW）——整案剔除")
    if rejected:
        return [], rejected          # 整案语义：任一违规，全部不执行
    return kept, []


def store_db_asserts(conn, task_id: int, round_no: int, rows: list[dict],
                     sidecar: str, engine: str) -> None:
    """db_asserts 第 29 表落库；幂等口径与 app_logs 一致（round≤1 清全任务、round>1 清同轮）。"""
    if round_no <= 1:
        conn.execute("DELETE FROM db_asserts WHERE task_id=?", (task_id,))
    else:
        conn.execute("DELETE FROM db_asserts WHERE task_id=? AND round_no=?", (task_id, round_no))
    for r in rows:
        conn.execute(
            """INSERT INTO db_asserts(task_id, round_no, sidecar, engine, sql_text,
                                      verdict, row_count, error, rejected_reason, elapsed_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, round_no, sidecar, engine, r.get("sql_text", ""),
             r.get("verdict", "failed"), r.get("row_count"),
             (r.get("error") or "")[:500], (r.get("rejected_reason") or "")[:500],
             int(r.get("elapsed_ms") or 0)))
    conn.commit()


def _exec_sql(runner, container, engine: str, database: str, sql: str):
    """sidecar 容器内执行单条只读 SQL；返回 ExecResult。

    exec_in 无环境注入参数 → SQL 经 /tmp 文件中转（heredoc 原文写入，不经 shell 展开）。"""
    runner.exec_in(container, ["sh", "-c", f"cat > /tmp/.dba_sql << 'DBAEOF'\n{sql}\nDBAEOF"],
                   timeout_s=15, workdir="/")
    if engine == "mariadb":
        cmd = ["sh", "-c",
               f"mariadb -h 127.0.0.1 -uroot -p\"$MARIADB_ROOT_PASSWORD\" {database} "
               f"< /tmp/.dba_sql 2>&1"]
    else:
        cmd = ["sh", "-c",
               f"psql -h 127.0.0.1 -U \"$POSTGRES_USER\" -d {database} < /tmp/.dba_sql 2>&1"]
    return runner.exec_in(container, cmd, timeout_s=60, workdir="/")


def db_assert(api_key: str, args: dict, task_id: int, runner) -> str:
    """SkillRegistry 入口：args={sql, round_no, container_name(sidecar), engine, database}。

    LLM 只生成验证 SQL（调用方节点负责 prompt）；本 skill 负责闸+执行+落库。
    返回 JSON 摘要（verdict 明细/拒绝留痕），叙述纪律与 log_analyze 同源。
    """
    container = runner.client.containers.get(args["container_name"])
    engine = args.get("engine", "mariadb")
    database = args.get("database") or "shop"
    round_no = int(args.get("round_no") or 1)
    sql_text = args.get("sql") or ""
    sidecar_name = args["container_name"]

    kept, rejected = _readonly_gate(sql_text)
    rows: list[dict] = []
    for reason in rejected:
        rows.append({"sql_text": sql_text[:500], "verdict": "rejected",
                     "row_count": None, "error": "", "elapsed_ms": 0,
                     "rejected_reason": reason})
    for sql in kept:
        t0 = time.time()
        try:
            r = _exec_sql(runner, container, engine, database, sql)
            elapsed = int((time.time() - t0) * 1000)
            if r.exit_code == 0:
                rows.append({"sql_text": sql, "verdict": "passed",
                             "row_count": len([l for l in r.stdout.splitlines() if l.strip()]),
                             "error": "", "elapsed_ms": elapsed})
            else:
                rows.append({"sql_text": sql, "verdict": "failed",
                             "row_count": None, "error": (r.stdout or r.stderr)[:500],
                             "elapsed_ms": elapsed})
        except Exception as e:  # noqa: BLE001 —— 执行失败如实落库，不炸主链路
            rows.append({"sql_text": sql, "verdict": "failed", "row_count": None,
                         "error": f"{type(e).__name__}: {e}",
                         "elapsed_ms": int((time.time() - t0) * 1000)})
    store_db_asserts(get_conn(), task_id, round_no, rows, sidecar=sidecar_name, engine=engine)
    passed = sum(1 for x in rows if x["verdict"] == "passed")
    failed = sum(1 for x in rows if x["verdict"] == "failed")
    rej = sum(1 for x in rows if x["verdict"] == "rejected")
    return json.dumps({"passed": passed, "failed": failed, "rejected": rej,
                       "rows": rows[:20]}, ensure_ascii=False)
