"""L4 验收辅助 leg（D5/D6）：验收条件 → GWT 场景清单（纯生成，无容器）。

L4 不自动执行：场景生成落库 pending → 人工确认（复用 /api/scenarios/{id}/confirm）
→ 确认后经 D6 转化器（POST /api/tasks/{tid}/l4/convert）产出可执行回归脚本。
"""
import json
import logging
import os

from .. import config
from ..skills.llm_usage import log_llm_call
from ..skills.scenario_gen import (ACCEPTANCE_SYSTEM, build_acceptance_prompt,
                                   parse_acceptance, save_scenarios)
from ..store.db import get_conn
from .l2 import _now

log = logging.getLogger(__name__)


def _leg_upsert_l4(conn, task_id: int, trace_from: int | None = None, **fields) -> int:
    row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level='L4'",
                       (task_id,)).fetchone()
    base = {"strategy": "assist", "status": "queued", "trace_from": trace_from}
    base.update({k: v for k, v in fields.items()})
    if row:
        sets = ", ".join(f"{k}=?" for k in base)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE id=?", (*base.values(), row["id"]))
        return row["id"]
    cols = ", ".join(base)
    cur = conn.execute(
        f"INSERT INTO test_levels(task_id, level, {cols}) VALUES (?, 'L4', {', '.join('?' for _ in base)})",
        (task_id, *base.values()))
    conn.commit()
    return cur.lastrowid


def run_l4_task(task_id: int, trace_from: int | None = None) -> dict:
    """L4 leg 入口（纯生成，无容器）：验收条件 → GWT 场景清单落库待确认。"""
    from .nodes import make_deps
    deps = make_deps()
    conn = deps.conn
    task = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                        (task_id,)).fetchone()
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    _leg_upsert_l4(conn, task_id, trace_from, status="running")
    cur = conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
                       (task_id,))
    conn.commit()

    def msg(content: str) -> None:
        from ..store.db import append_agent_message
        append_agent_message(conn, cur.lastrowid, "assistant", content)   # B2：原子 seq 取号

    # 会话收口（2026-11-15 闲时找bug）：成功/诚实跳过/异常三路都不悬开——
    # 整段 try/finally 关闭（l4 是唯一从不写 ended_at 的 leg）
    try:
        from ..skills.llm_profile import effective_key, missing_key_message
        api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析
        conds = [dict(r) for r in conn.execute(
            "SELECT condition, priority, method_hint FROM test_conditions "
            "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
        if not api_key:
            reason = missing_key_message({"provider": "未配置", "key_env": "见设置页"})
            _leg_upsert_l4(conn, task_id, status="skipped", verdict="skipped",
                           stop_reason=reason + "：验收场景生成跳过（诚实降级）",
                           finished_at=_now())
            msg(f"[L4·gen] {reason}：验收场景生成跳过（诚实降级）")
            return {"verdict": "skipped", "stop_reason": "无 LLM 密钥"}
        if not conds:
            _leg_upsert_l4(conn, task_id, status="skipped", verdict="skipped",
                           stop_reason="无验收条件（plan 未产出 test_conditions），L4 无从拆解",
                           finished_at=_now())
            msg("[L4·gen] 无验收条件（plan/RTM 未产出）：本级诚实跳过")
            return {"verdict": "skipped", "stop_reason": "无验收条件"}
        endpoints = [dict(r) for r in conn.execute(
            """SELECT method, path, params, request_schema FROM interface_endpoints
               WHERE task_id=? ORDER BY id""", (task_id,)).fetchall()]
        if not endpoints:
            _leg_upsert_l4(conn, task_id, status="skipped", verdict="skipped",
                           stop_reason="无端点清单（验收回归转化依赖 API 面），本级诚实跳过",
                           finished_at=_now())
            msg("[L4·gen] 无端点清单：本级诚实跳过")
            return {"verdict": "skipped", "stop_reason": "无端点清单"}
        for e in endpoints:
            e["params"] = json.loads(e.get("params") or "[]")
            e["request_schema"] = json.loads(e["request_schema"]) if e.get("request_schema") else None

        from ..skills.llm_client import make_chat
        llm = make_chat(phase="l4_gen_scenarios", api_key=api_key, temperature=0.2,
                        timeout=90, max_retries=2)
        resp = llm.invoke([("system", ACCEPTANCE_SYSTEM),
                           ("user", build_acceptance_prompt(conds, endpoints))])
        log_llm_call(task_id, "l4_gen_scenarios", resp,
                     ctx_budget=getattr(llm, "_ctx_budget", None),
                     base_url=getattr(llm, "_base_url", ""))
        valid, rejected = parse_acceptance(resp.content, endpoints)
        n = save_scenarios(conn, task_id, valid, level="L4")
        msg(f"[L4·gen] 验收场景 {n} 个落库待确认"
            + (f"；剔除 {len(rejected)} 条：{'；'.join(rejected[:3])}" if rejected else ""))
        _leg_upsert_l4(conn, task_id, status="done", verdict="assist",
                       summary=json.dumps({"scenarios": n, "rejected": len(rejected)},
                                          ensure_ascii=False),
                       finished_at=_now())
        msg(f"[L4·report] 本级终态 assist｜{n} 个场景待人工确认（确认后可经 D6 转化回归脚本）")
        return {"verdict": "assist", "scenarios": n, "rejected": len(rejected)}
    finally:
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') "
            "WHERE id=? AND ended_at IS NULL", (cur.lastrowid,))
        conn.commit()

