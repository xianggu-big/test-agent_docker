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
- 调用方（T3 接线）负责 try/except 包裹：账本失败只 warning，绝不打断主链路。

attempt_no 规则（契约 §0）：single/prep/d1_review=新尝试（该任务 max+1）；
exec=归入该任务最新尝试。时间戳：新数据一律 epoch 毫秒（与历史秒级 UTC 分账，
契约 §1.4 不混算）。
"""
from __future__ import annotations

import json
import time

# 任务执行中状态：reconcile 判 orphan 时"不在这些状态=已不在执行态"
_EXECUTING_STATUSES = ("cloning", "analyzing", "provisioning", "generating",
                       "running", "reporting")

DECISION_JSON_CAP = 8000  # 模型原始动作 JSON 封顶（DDL 注释同源）


def _now_ms() -> int:
    return int(time.time() * 1000)


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
