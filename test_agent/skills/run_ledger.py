# -*- coding: utf-8 -*-
"""运行级账本（学院达标 T2，2026-11-25）：agent_runs / agent_steps 两表的纯函数层。

设计=docs/学院达标-任务规划与设计-20261124.md §6：
- **账本是旁路**：只多写两张表，不改提示词、不改图、不改任何现有输出。
  开关 config.RUN_LEDGER_ENABLED（MTA_RUN_LEDGER，默认关）由调用方判——本模块
  不读旗标，纯函数语义，关态=调用方根本不进来。
- **防双轨**：agent_steps 只存"决策与链接"，token/耗时/执行结果仍以 llm_calls、
  skill_invocations 为真源，外键指过去。
- **自愈对账**：reconcile_runs 把残留 running 的 run 按证据收口（会话已 ended →
  按任务态定；队列已 finished → 同步；任务已不在执行态 → orphan）。

T3 采集接线（2026-11-25）补充的调用方入口：
- `guarded(fn, ...)`:旗标关=零调用（关态逐字节不变）；写失败只 warning 不炸主链路。
- `ledger_start_run(...)`:builder 各入口的统一建行口——旗标关时退回旧行为
  （裸 INSERT agent_sessions），开时建两行；失败降级同旧行为。
- `set_current_queue_id()`:queue worker 认领后经 ContextVar 传队列行 id，
  start_run 落 agent_runs.queue_id（不改 builder/queue 函数签名）。
- `build_config_json(...)`:配置快照（契约 §4）——无它不同批次不可比。
- `build_run_summary(...)`:终态快照（重跑 delete-first 会清任务级表，
  不冻结旧运行无法回放）。

attempt_no 规则（契约 §0）：single/prep/d1_review=新尝试（该任务 max+1）；
exec=归入该任务最新尝试。时间戳：新数据一律 epoch 毫秒（与历史秒级 UTC 分账，
契约 §1.4 不混算）。
"""
from __future__ import annotations

import contextvars
import json
import logging
import subprocess
import time

from .. import config

log = logging.getLogger(__name__)

# 任务执行中状态：reconcile 判 orphan 时"不在这些状态=已不在执行态"
_EXECUTING_STATUSES = ("cloning", "analyzing", "provisioning", "generating",
                       "running", "reporting")

DECISION_JSON_CAP = 8000  # 模型原始动作 JSON 封顶（DDL 注释同源）

# queue worker 认领的队列行 id（同线程 ContextVar；start_run 落 queue_id）
_current_queue_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "mta_current_queue_id", default=None)


def set_current_queue_id(qid: int | None) -> None:
    """queue worker 在派发前置/复位（None）。普通调用方不用管。"""
    _current_queue_id.set(qid)


def _now_ms() -> int:
    return int(time.time() * 1000)


def guarded(fn, *args, **kwargs):
    """账本写入统一入口：旗标关=零调用（关态逐字节不变）；写失败只 warning。

    返回 fn 的返回值或 None（关/失败）。"""
    if not config.RUN_LEDGER_ENABLED:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:      # noqa: BLE001 —— 账本是旁路，绝不打断主链路
        log.warning("[agent-run] 账本写入失败 %s: %s", getattr(fn, "__name__", fn), e)
        return None


def build_config_json(conn, image: str | None = None) -> str:
    """配置快照（契约 §1.4/§4）：模型档、提示词版本、旗标、镜像、代码版本。
    失败容忍——快照缺项可比性下降但主链路照常。"""
    snapshot: dict = {}
    try:
        from .llm_profile import current
        snapshot["llm_profile"] = current(conn)
    except Exception as e:      # noqa: BLE001
        snapshot["llm_profile"] = f"unavailable: {type(e).__name__}"
    try:
        from .prompt_library import AGENT_LOOP_PROMPT_VERSION
        snapshot["prompt_version"] = AGENT_LOOP_PROMPT_VERSION
    except Exception:           # noqa: BLE001
        pass
    snapshot["flags"] = {
        "agent_loop_enabled": bool(config.AGENT_LOOP_ENABLED),
        "run_ledger_enabled": bool(config.RUN_LEDGER_ENABLED),
        "env_gate": bool(config.ENV_GATE_ENABLED),
        "diff_gate": bool(config.DIFF_GATE_ENABLED),
    }
    if image:
        snapshot["image"] = image
    try:
        snapshot["git_head"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            timeout=5, cwd=config.DATA_DIR.parent).stdout.strip() or None
    except Exception:           # noqa: BLE001
        snapshot["git_head"] = None
    return json.dumps(snapshot, ensure_ascii=False)


def start_run(conn, task_id: int, kind: str, goal_text: str | None = None,
              queue_id: int | None = None, prompt_version: str | None = None,
              config_json: str | None = None) -> tuple[int, int]:
    """建会话 + run 两行（builder 各入口的会话创建统一走这里），返回 (session_id, run_id)。"""
    cur = conn.execute(
        "INSERT INTO agent_sessions(task_id) VALUES (?)", (task_id,))
    session_id = cur.lastrowid
    if kind == "exec":
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS m FROM agent_runs WHERE task_id=?",
            (task_id,)).fetchone()
        attempt_no = max(row["m"], 1)
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS m FROM agent_runs WHERE task_id=?",
            (task_id,)).fetchone()
        attempt_no = row["m"] + 1
    cur = conn.execute(
        """INSERT INTO agent_runs(task_id, session_id, attempt_no, kind, queue_id,
                                  goal_text, started_ms, status, prompt_version, config_json)
           VALUES (?,?,?,?,?,?,?, 'running', ?, ?)""",
        (task_id, session_id, attempt_no, kind, queue_id, goal_text,
         _now_ms(), prompt_version, config_json))
    conn.commit()
    return session_id, cur.lastrowid


def ledger_start_run(conn, task_id: int, kind: str, goal_text: str | None = None,
                     prompt_version: str | None = None,
                     image: str | None = None) -> tuple[int, int | None]:
    """builder 各入口的统一建行口（T3）。

    - 旗标关：旧行为逐字节不变——裸 INSERT agent_sessions，返回 (sid, None)。
    - 旗标开：start_run 建两行（run 行带 goal/prompt_version/config_json/queue_id
      [ContextVar]），返回 (sid, rid)。任何账本失败降级为旧行为（主链路优先）。
    """
    if not config.RUN_LEDGER_ENABLED:
        cur = conn.execute(
            "INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
            (task_id,))
        conn.commit()
        return cur.lastrowid, None
    try:
        return start_run(conn, task_id, kind=kind, goal_text=goal_text,
                         queue_id=_current_queue_id.get(),
                         prompt_version=prompt_version,
                         config_json=build_config_json(conn, image=image))
    except Exception as e:      # noqa: BLE001 —— 降级旧行为
        log.warning("[agent-run] start_run 失败，降级裸会话 task=%s: %s", task_id, e)
        cur = conn.execute(
            "INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
            (task_id,))
        conn.commit()
        return cur.lastrowid, None


def run_init_keys(conn, run_id: int | None) -> dict:
    """run_id 非空时取 init 所需的账本键（run_id/run_started_ms）——未声明会被
    LangGraph 静默过滤（三重实锤的老坑），所以 builder 显式放进 init。"""
    if run_id is None:
        return {}
    row = conn.execute("SELECT started_ms FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    return {"run_id": run_id, "run_started_ms": row["started_ms"] if row else None}


def set_prompt_version(conn, run_id: int, version: str | None) -> None:
    """决策循环实际使用的提示词版本写回 run 行（§7 采集点）。"""
    conn.execute("UPDATE agent_runs SET prompt_version=? WHERE id=?", (version, run_id))
    conn.commit()


def build_run_summary(conn, task_id: int) -> dict:
    """终态快照（§6.1）：重跑 delete-first 会清任务级表，不冻结旧运行无法回放。

    只读查询：评估轮次行、报告行关键数字、用例结果按状态计数、终态与 error、
    决策循环消息行（[strategy]/[diagnose]）。全部 try 包裹——快照缺项主链路照常。"""
    summary: dict = {}
    try:
        summary["evaluation_rounds"] = [dict(r) for r in conn.execute(
            "SELECT round_no, passed, failed, skipped, coverage_line, verdict, "
            "failure_kind, stop_reason FROM evaluation_rounds WHERE task_id=? "
            "ORDER BY id", (task_id,)).fetchall()]
    except Exception as e:      # noqa: BLE001
        summary["evaluation_rounds"] = f"unavailable: {type(e).__name__}"
    try:
        summary["report"] = [dict(r) for r in conn.execute(
            "SELECT passed, failed, skipped, coverage_line, defects FROM test_reports "
            "WHERE task_id=? ORDER BY id DESC LIMIT 3", (task_id,)).fetchall()]
    except Exception as e:      # noqa: BLE001
        summary["report"] = f"unavailable: {type(e).__name__}"
    try:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN r.status='passed' THEN 1 ELSE 0 END) AS passed,
                 SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed,
                 SUM(CASE WHEN r.status='error' THEN 1 ELSE 0 END) AS error,
                 SUM(CASE WHEN r.status='skipped' THEN 1 ELSE 0 END) AS skipped,
                 COUNT(*) AS total
               FROM test_case_results r JOIN test_scripts s ON s.id=r.script_id
               WHERE s.task_id=?""", (task_id,)).fetchone()
        summary["case_counts"] = {k: row[k] for k in
                                  ("passed", "failed", "error", "skipped", "total")}
    except Exception as e:      # noqa: BLE001
        summary["case_counts"] = f"unavailable: {type(e).__name__}"
    try:
        task = conn.execute("SELECT status, error FROM test_tasks WHERE id=?",
                            (task_id,)).fetchone()
        summary["final"] = {"status": task["status"] if task else None,
                            "error": task["error"] if task else None}
    except Exception as e:      # noqa: BLE001
        summary["final"] = f"unavailable: {type(e).__name__}"
    try:
        summary["loop_msgs"] = [r["content"][:300] for r in conn.execute(
            """SELECT m.content FROM agent_messages m JOIN agent_sessions s
               ON s.id=m.session_id WHERE s.task_id=?
                 AND (m.content LIKE '[strategy]%' OR m.content LIKE '[diagnose]%')
               ORDER BY m.id""", (task_id,)).fetchall()]
    except Exception as e:      # noqa: BLE001
        summary["loop_msgs"] = f"unavailable: {type(e).__name__}"
    return summary


def finish_run(conn, run_id: int, status: str, stop_reason: str | None = None,
               summary_json: str | None = None, metrics_json: str | None = None,
               outcome: str | None = None) -> None:
    """收口 run 行（ended_ms/status/stop_reason/summary/metrics/outcome）+ 同步会话 ended_at。

    status ∈ running/done/failed/cancelled/orphan（DDL CHECK）。正常结束路径与
    reconcile 都走这里；ended_ms 幂等（重复收口不回退首值）。"""
    conn.execute(
        """UPDATE agent_runs SET ended_ms=COALESCE(ended_ms, ?), status=?,
                                 stop_reason=COALESCE(?, stop_reason),
                                 summary_json=COALESCE(?, summary_json),
                                 metrics_json=COALESCE(?, metrics_json),
                                 outcome=COALESCE(?, outcome)
           WHERE id=?""",
        (_now_ms(), status, stop_reason, summary_json, metrics_json, outcome, run_id))
    conn.execute(
        """UPDATE agent_sessions SET ended_at=COALESCE(ended_at, datetime('now'))
           WHERE id=(SELECT session_id FROM agent_runs WHERE id=?)""", (run_id,))
    conn.commit()


def record_step(conn, run_id: int, mode: str, loop_no: int, step_no: int,
                action_type: str, capability: str | None = None,
                args: dict | None = None, decision_json: str | None = None,
                allowed: int | None = None, deny_reason: str | None = None,
                llm_call_id: int | None = None, invocation_id: int | None = None,
                result_status: str | None = None, result_summary: str | None = None,
                evidence: list | None = None, decision_ms: int | None = None,
                exec_ms: int | None = None) -> int:
    """决策循环逐步一行（denied / unparseable / finish 都写）。decision_json 封顶
    8000 字符；evidence 只存指针不存原文。返回步骤行 id。"""
    if decision_json and len(decision_json) > DECISION_JSON_CAP:
        decision_json = decision_json[:DECISION_JSON_CAP]
    cur = conn.execute(
        """INSERT INTO agent_steps(run_id, mode, loop_no, step_no, action_type,
                                   capability, args_json, decision_json, allowed,
                                   deny_reason, llm_call_id, invocation_id,
                                   result_status, result_summary, evidence_json,
                                   decision_ms, exec_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, mode, loop_no, step_no, action_type, capability,
         json.dumps(args, ensure_ascii=False) if args is not None else None,
         decision_json, allowed, deny_reason, llm_call_id, invocation_id,
         result_status,
         result_summary[:500] if result_summary else None,
         json.dumps(evidence, ensure_ascii=False) if evidence else None,
         decision_ms, exec_ms))
    conn.commit()
    return cur.lastrowid


def reconcile_runs(conn) -> int:
    """自愈对账（§6.3）：把残留 running 的 run 按证据收口。返回收口条数。

    调用点：QueueWorker.start（接在 _recover 之后）与运行列表 API 读路径——
    图异常、进程被杀、漏接的退出路径都不会留下永久 running。"""
    rows = conn.execute(
        "SELECT id, task_id, session_id, queue_id FROM agent_runs WHERE status='running'"
    ).fetchall()
    n = 0
    for r in rows:
        sess = conn.execute("SELECT ended_at FROM agent_sessions WHERE id=?",
                            (r["session_id"],)).fetchone()
        task = conn.execute("SELECT status FROM test_tasks WHERE id=?",
                            (r["task_id"],)).fetchone()
        task_status = task["status"] if task else None
        # ① 会话已有 ended_at → 按任务态定 done/failed/cancelled
        if sess and sess["ended_at"]:
            status = ("cancelled" if task_status == "cancelled"
                      else "done" if task_status == "done" else "failed")
            finish_run(conn, r["id"], status=status,
                       stop_reason="[reconcile] 会话已结束，按任务终态收口")
            n += 1
            continue
        # ② 队列行已 finished → 同步其状态
        if r["queue_id"]:
            q = conn.execute("SELECT status FROM exec_queue WHERE id=?",
                             (r["queue_id"],)).fetchone()
            if q and q["status"] in ("done", "failed", "cancelled"):
                finish_run(conn, r["id"], status=q["status"],
                           stop_reason=f"[reconcile] 队列行 {q['status']} 同步")
                n += 1
                continue
        # ③ 任务已不在执行态 → orphan
        if task_status is not None and task_status not in _EXECUTING_STATUSES:
            finish_run(conn, r["id"], status="orphan",
                       stop_reason=f"[reconcile] 任务态 {task_status} 非执行态收口")
            n += 1
    return n
