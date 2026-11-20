"""Agent Loop 有界决策循环（2026-11-20 设计稿 §3.3/§3.4；MTA_AGENT_LOOP 默认关）。

固定图拓扑内的**节点内**循环——模型决定"节点内调哪个能力"，永不决定节点间流转
（开题口径"LLM 规划作为节点产物而非流程控制"）。协议=JSON action（call/finish），
不走 function calling（对码 V2：RobustChat.__getattr__ 透传会让 bind_tools 返回
裸 ChatOpenAI 丢重试/402 降级/记账——改用 _loads_tolerant 既有漂移容错资产）。

停止条件五+一（§6.4+调研修正 3）：goal_met / budget / no_new_evidence×2 /
repeated_action / hard_error / policy_deadlock（连续 3 次被拒=策略与权限面失配，
OpenHands Stuck Detector 同款）。
LLM 记账走 make_chat(phase=agent_*)→log_llm_call 现有管线；能力执行走
SkillRegistry.invoke（复用 skill_invocations 审计台账——实施附录件 1.3 防双轨）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time

from .. import config
from . import tool_policy
from .capabilities import (CapResult, TaskCtx, action_fingerprint, capability_menu,
                           find_capability, normalize_result)
from .llm_client import make_chat          # 模块级：测试 monkeypatch 锚点
from .llm_usage import log_llm_call
from .prompt_library import AGENT_DECIDE_SYSTEM, estimate_tokens

log = logging.getLogger(__name__)


def _extract_json(text: str):
    """模型输出 → JSON 对象（宽容解析：代码围栏/前缀散文/字面量重试）。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = []
    if m:
        cands.append(m.group(1))
    m2 = re.search(r"\{.*\}", text, re.S)
    if m2:
        cands.append(m2.group(0))
    cands.append(text.strip())
    from .gen_tests import _loads_tolerant
    for cand in cands:
        try:
            data = _loads_tolerant(cand)
        except Exception:       # noqa: BLE001
            data = None
        if isinstance(data, dict):
            return data
    return None


def _evidence_fp(capability: str, result: CapResult) -> str:
    """结果指纹（no_new_evidence 判据）：不含路径——不同文件同内容=同一结果
    （证据语义重复），路径级差异不算新证据。"""
    shas = sorted(e.get("sha256", "") for e in result.evidence)
    blob = json.dumps([capability, result.status, result.metrics, shas],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _execute_capability(deps, ctx: TaskCtx, cap, args: dict) -> CapResult:
    """能力执行：统一经 SkillRegistry.invoke（复用 skill_invocations 审计台账——
    每个 Capability 注册同名 Skill；R0→low/R1→medium 映射避免撞 0-B 高险确认门）。
    deps.registry 不可得（纯单测形态）→ 直接调 handler（零审计，仅测试用）。"""
    reg = getattr(deps, "registry", None)

    def _run(_task_id, _args):
        r = normalize_result(cap.handler(ctx, dict(_args)))
        return json.dumps(dataclasses.asdict(r), ensure_ascii=False)

    if reg is None:
        return CapResult(**json.loads(_run(ctx.task_id, args)))
    from .registry import Skill
    skill = Skill(name=cap.id, description=cap.description, func=_run,
                  risk_level="low" if cap.risk == "R0" else "medium",
                  timeout_ms=cap.timeout_s * 1000, arg_schema=cap.inputs_schema)
    reg.register(skill)
    res = reg.invoke(ctx.task_id, skill, args)
    if res.status == "denied":
        return CapResult(status="denied", summary=(res.error or "denied")[:200])
    if res.status == "timeout":
        return CapResult(status="timeout", summary=(res.error or "timeout")[:200])
    try:
        data = json.loads(res.output or "{}")
        return CapResult(**{k: data.get(k) for k in
                            ("status", "summary", "metrics", "evidence",
                             "diagnostics", "raw_exit")})
    except Exception as e:      # noqa: BLE001
        return CapResult(status="failed", summary=f"结果解析失败：{type(e).__name__}")


def build_observation(ctx: TaskCtx, goal: str, mode: str, menu: list[dict],
                      recent_evidence: list[dict], budget: dict,
                      used: dict) -> str:
    """观察包（token 封顶）：目标+任务上下文+菜单+近 3 条证据摘要+剩余预算。
    摘要级——原始大文本永远不进观察（证据文件留盘按需引用）。"""
    snap = ctx.state_snapshot or {}
    ev_lines = []
    for e in recent_evidence:
        ev_lines.append(f"- step{e.get('step')} {e.get('capability')}={e.get('status')}: "
                        f"{(e.get('summary') or '')[:120]}")
    menu_lines = "\n".join(f"- {m['id']} ({m['risk']}) {m['description']}" for m in menu)
    obs = (
        f"## 目标\n{goal[:600]}\n\n"
        f"## 任务上下文\n语言={ctx.primary_language} 工作区={ctx.workspace.name} "
        f"轮次={snap.get('round_no')} 冻结={snap.get('verify_mode')} 静态={snap.get('static_only')}\n"
        f"路由范围={snap.get('index_scope_paths') or '全库'}\n\n"
        f"## 能力菜单（只能从中选）\n{menu_lines}\n\n"
        f"## 近期证据\n" + ("\n".join(ev_lines) or "（尚无）") + "\n\n"
        f"## 剩余预算\n步数 {used.get('steps', 0)}/{budget.get('max_steps')} "
        f"token {used.get('tokens', 0)}/{budget.get('max_tokens')} "
        f"秒 {used.get('seconds', 0)}/{budget.get('max_seconds')}\n\n"
        "## 输出\n只输出一个 JSON 对象（call 或 finish 形态，见系统指令）。"
    )
    return obs[:12000]          # 观察包硬封顶（防菜单/证据膨胀吃满上下文）


def run_tool_loop(deps, ctx: TaskCtx, goal: str, mode: str, budget: dict,
                  phase: str) -> dict:
    """有界决策循环。mode：strategy（规划前观察）| diagnose（失败证据诊断）。
    返回 {"stop", "steps", "used", "evidence", "payload"}——payload=finish 动作
    原文（strategy/diagnosis 由调用方提取）。fail-closed：任何循环级异常 →
    stop("hard_error") 且证据如实保留。"""
    menu = capability_menu(ctx)
    evidence: list[dict] = []
    seen_action_fps: dict[str, int] = {}
    result_fps: set[str] = set()
    new_evidence_streak = 0
    denied_streak = 0
    unparseable = 0
    used = {"steps": 0, "tokens": 0, "seconds": 0}
    t0 = time.monotonic()

    def _stop(reason: str, payload=None) -> dict:
        return {"stop": reason, "steps": used["steps"], "used": used,
                "evidence": evidence, "payload": payload or {}}

    if not menu:
        return _stop("hard_error", {"summary": "能力菜单为空（配置异常）"})
    try:
        llm = make_chat(phase=phase, temperature=0.2, timeout=120, task_id=ctx.task_id)
    except Exception as e:      # noqa: BLE001 —— 构造失败=hard_error（证据保留）
        log.warning("agent loop make_chat 失败 task=%s: %s", ctx.task_id, e)
        return _stop("hard_error", {"summary": f"LLM 构造失败：{type(e).__name__}"})

    max_steps = int(budget.get("max_steps") or 1)
    for step in range(1, max_steps + 1):
        used["steps"] = step
        used["seconds"] = int(time.monotonic() - t0)
        obs = build_observation(ctx, goal, mode, menu, evidence[-3:], budget, used)
        try:
            resp = llm.invoke([("system", AGENT_DECIDE_SYSTEM), ("user", obs)])
        except Exception as e:  # noqa: BLE001 —— LLM 调用异常（402 分类已在包装层）
            log.warning("agent loop LLM 异常 task=%s: %s", ctx.task_id, e)
            return _stop("hard_error",
                         {"summary": f"LLM 调用异常：{type(e).__name__}: {str(e)[:120]}"})
        used["tokens"] += estimate_tokens(obs) + estimate_tokens(getattr(resp, "content", "") or "")
        log_llm_call(ctx.task_id, phase, resp,
                     ctx_budget=getattr(llm, "_ctx_budget", None),
                     base_url=getattr(llm, "_base_url", ""))
        action = _extract_json(getattr(resp, "content", "") or "")
        if action is None:
            unparseable += 1
            if unparseable >= 2:
                return _stop("hard_error", {"summary": "模型输出两次不可解析（原始已留档 llm_parse_fail/）"})
            evidence.append({"step": step, "capability": "-", "status": "unparseable",
                             "summary": "输出不可解析，重试一次"})
            continue
        unparseable = 0
        if action.get("action") == "finish":
            return _stop("goal_met", payload=action)
        decision = tool_policy.authorize(action, ctx, menu=menu,
                                         seen_fps=seen_action_fps,
                                         budget_used={**used,
                                                      "max_steps": budget.get("max_steps"),
                                                      "max_tokens": budget.get("max_tokens"),
                                                      "max_seconds": budget.get("max_seconds")})
        if decision.stop:
            return _stop(decision.stop_reason)
        if not decision.allowed:
            evidence.append({"step": step, "capability": action.get("capability"),
                             "status": "denied", "summary": decision.reason[:200]})
            denied_streak += 1
            if denied_streak >= 3:
                return _stop("policy_deadlock",
                             {"summary": "连续 3 次被拒——策略与权限面失配（OpenHands Stuck 同款判据）"})
            continue
        denied_streak = 0
        afp = action_fingerprint(action["capability"], action["args"])
        seen_action_fps[afp] = seen_action_fps.get(afp, 0) + 1
        cap = find_capability(ctx, action["capability"])
        try:
            result = _execute_capability(deps, ctx, cap, action.get("args") or {})
        except Exception as e:  # noqa: BLE001 —— handler 异常降级为 failed 结果
            result = CapResult(status="failed",
                               summary=f"{type(e).__name__}: {str(e)[:150]}")
        result = normalize_result(result)
        rfp = _evidence_fp(action["capability"], result)
        if rfp in result_fps:
            new_evidence_streak += 1
            if new_evidence_streak >= 2:
                evidence.append({"step": step, "capability": cap.id,
                                 "status": result.status,
                                 "summary": result.summary})
                return _stop("no_new_evidence",
                             {"summary": "连续两次无新证据（结果指纹重复）"})
        else:
            result_fps.add(rfp)
            new_evidence_streak = 0
        evidence.append({"step": step, "capability": cap.id, "status": result.status,
                         "summary": result.summary, "metrics": result.metrics,
                         "evidence": result.evidence})
    used["seconds"] = int(time.monotonic() - t0)
    return _stop("budget", {"summary": "步数耗尽——已获证据如实保留"})
