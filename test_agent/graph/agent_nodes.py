"""Agent Loop 图节点（2026-11-20 设计稿 §3.5；MTA_AGENT_LOOP 默认关=图形态不变）。

strategy（主图 analyze→strategy→plan）：规划前观察（R0 能力小循环）→ 产出
agent_strategy（节点产物而非流程控制——plan 只读作上下文，测试条件仍锚真值清单）。
diagnose（repair_gen 分支前，round≤DIAG_MAX_ROUNDS）：只读小循环（菜单收窄 R0）
→ agent_diagnosis——只注入提示词，**永不写 verdict**（归因真值不外包）。
366 教训：跨节点键必须同时进 state 与返回值 out。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from ..skills.capabilities import TaskCtx, capability_menu
from ..skills.tool_loop import run_tool_loop

log = logging.getLogger(__name__)


def _msg(conn, session_id: int, content: str) -> None:
    """轨迹写进当前会话日志（前端日志流可见）。失败不炸主链路。"""
    try:
        seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                           (session_id,)).fetchone()["n"]
        conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) "
                     "VALUES (?,?,?,?)", (session_id, seq, "assistant", content))
        conn.commit()
    except Exception as e:      # noqa: BLE001
        log.warning("agent 节点留痕失败: %s", e)


def _make_ctx(deps, state: dict, mode: str) -> TaskCtx:
    tid = state["task_id"]
    ws = Path(state.get("workspace") or (config.DATA_DIR / "workspaces" / f"task_{tid}"))
    return TaskCtx(
        task_id=tid, session_id=state["session_id"], workspace=ws,
        container_name=state.get("container_name") or f"mta-run-{tid}",
        primary_language=state.get("primary_language") or "",
        image=state.get("image") or "",
        state_snapshot=dict(state), conn=deps.conn, runner=deps.runner,
        artifacts_dir=config.DATA_DIR / "artifacts" / f"task-{tid}" / "agent-loop",
        agent_mode=mode)


def make_agent_nodes(deps):
    """返回 {"strategy": fn, "diagnose": fn}——builder 仅在 MTA_AGENT_LOOP 开时注册。"""
    conn = deps.conn

    def strategy_node(state: dict) -> dict:
        tid = state["task_id"]
        ctx = _make_ctx(deps, state, "strategy")
        budget = {"max_steps": config.AGENT_LOOP_MAX_STEPS,
                  "max_tokens": config.AGENT_LOOP_MAX_TOKENS,
                  "max_seconds": config.AGENT_LOOP_MAX_SECONDS,
                  "observe_cap": config.AGENT_LOOP_OBSERVE_CAP}   # v1.1：观察子预算
        result = run_tool_loop(deps, ctx, goal=state.get("requirement") or "",
                               mode="strategy", budget=budget, phase="agent_strategy")
        strategy = (result.get("payload") or {}).get("strategy") or {}
        grants = [m["id"] for m in capability_menu(ctx)]
        n_pri = len(strategy.get("priorities") or [])
        _msg(conn, state["session_id"],
             f"[strategy] 决策循环 {result.get('stop')}（{result.get('steps')} 步）"
             f"——策略 {n_pri} 项优先级"
             + ("，plan 将参考（测试条件仍锚真值清单）" if n_pri else "（无有效策略，按默认路线走）"))
        out = {"agent_loop_enabled": True,
               "agent_strategy": strategy,
               "agent_steps": result.get("steps", 0),
               "agent_budget": {**budget, "used_tokens": (result.get("used") or {}).get("tokens", 0),
                                "used_seconds": (result.get("used") or {}).get("seconds", 0)},
               "agent_evidence": result.get("evidence", []),
               "agent_capability_grants": grants,
               "agent_loop_stop": result.get("stop"),
               "agent_prompt_version": result.get("prompt_version") or "?"}
        return out   # 366 教训：跨节点键必须进返回值（strategy 只读 state 的写入方就是这里）

    def diagnose_node(state: dict) -> dict:
        tid = state["task_id"]
        ctx = _make_ctx(deps, state, "diagnose")
        budget = {"max_steps": config.AGENT_LOOP_DIAG_STEPS,
                  "max_tokens": max(config.AGENT_LOOP_MAX_TOKENS // 3, 10000),
                  "max_seconds": config.AGENT_LOOP_MAX_SECONDS}
        result = run_tool_loop(deps, ctx,
                               goal="诊断上一轮失败：归类并给出下一步建议（只读证据）",
                               mode="diagnose", budget=budget, phase="agent_diagnose")
        diagnosis = (result.get("payload") or {}).get("diagnosis") or {}
        _msg(conn, state["session_id"],
             f"[diagnose] 只读诊断 {result.get('stop')}（{result.get('steps')} 步）"
             f"——kind={diagnosis.get('kind')} 置信={diagnosis.get('confidence')}"
             + ("；结论注入生成提示词（不改 verdict）" if diagnosis else "（无结论，按原路线补测）"))
        out = {"agent_diagnosis": diagnosis,
               "agent_steps": (state.get("agent_steps") or 0) + result.get("steps", 0),
               "agent_evidence": (state.get("agent_evidence") or []) + result.get("evidence", []),
               "agent_loop_stop": result.get("stop")}
        return out   # 铁律：永不写 verdict——诊断只注入提示词

    return {"strategy": strategy_node, "diagnose": diagnose_node}
