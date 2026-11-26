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
from . import run_ledger
from . import tool_policy
from .capabilities import (CapResult, TaskCtx, action_fingerprint, capability_menu,
                           find_capability_extended, normalize_result)
from .llm_client import make_chat          # 模块级：测试 monkeypatch 锚点
from .llm_usage import log_llm_call
from .prompt_library import (AGENT_LOOP_PROMPT_VERSION, build_agent_decide_system,
                             estimate_tokens)

log = logging.getLogger(__name__)


def _ledger_step(ctx: TaskCtx, **kw):
    """运行账本步骤行（T3 采集）：ctx.run_id 为空（账本关/降级）=零写入；
    写失败 guarded 只 warning。键名见 run_ledger.record_step。"""
    if ctx.run_id is None:
        return None
    return run_ledger.guarded(run_ledger.record_step, ctx.conn, ctx.run_id, **kw)


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


# AL-5（2026-11-26 设计 F-AL5）："具体"判定——设计报告 §1.1 口径。
_GENERIC_BANNED = {"tests", "test", "src", "lib", "app", "unit", "case", "cases",
                   "testing", "coverage", "function", "code", "file", "files"}
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{2,}")
_IDENT_STOP = {"the", "and", "for", "with", "this", "that", "should", "then",
               "when", "where", "each", "case", "test", "tests", "testing",
               "expect", "assert", "input", "output", "return", "value",
               "check", "cover", "coverage", "missing", "gap", "lack", "need",
               "add", "run", "unit", "error", "fail", "failed", "none", "null",
               "true", "false", "example", "increase", "improve", "enhance",
               "ensure", "verify", "补充", "覆盖", "测试", "验证"}


def _strategy_is_generic(strategy: dict | None) -> bool:
    """策略是否全是泛指目标（设计 §1.1"具体"判定的机器闸）。
    具体=优先项的 why/feedback/targets 里抽到至少一个标识符（字母开头、长度 ≥3、
    去语言关键字/英文填充词）或路径/带下划线/带数字形符号；`.`、`./...`、`tests`、
    单独项目名不算。全部优先项都抽不到=泛指。无策略/无优先项=泛指。"""
    if not isinstance(strategy, dict) or not (strategy.get("priorities") or []):
        return True
    for p in strategy["priorities"]:
        texts = [str(p.get("why") or "")]
        ah = p.get("args_hint") or {}
        if isinstance(ah, dict):
            texts.append(str(ah.get("feedback") or ""))
            texts.append(" ".join(str(t) for t in (ah.get("targets") or [])))
        blob = " ".join(texts)
        for m in _IDENT_RE.findall(blob):
            token = m.lower().strip("./-_")
            if not token or token in _GENERIC_BANNED or token in _IDENT_STOP:
                continue
            # 路径形（calc.js / src/x）或带下划线/带数字=具体符号
            if "." in m or "/" in m or "_" in m or any(ch.isdigit() for ch in m):
                return False
            if len(token) >= 3:
                return False
    return True


def _execute_capability(deps, ctx: TaskCtx, cap, args: dict) -> tuple[CapResult, int | None]:
    """能力执行：统一经 SkillRegistry.invoke（复用 skill_invocations 审计台账——
    每个 Capability 注册同名 Skill；R0→low/R1→medium 映射避免撞 0-B 高险确认门）。
    返回 (结果, invocation_id)——invocation_id 是运行账本 agent_steps 的外键
    （T3：此前已有但没往上抛）。deps.registry 不可得（纯单测形态）→ 直接调
    handler（零审计，仅测试用，invocation_id=None）。"""
    reg = getattr(deps, "registry", None)

    def _run(_task_id, _args):
        r = normalize_result(cap.handler(ctx, dict(_args)))
        return json.dumps(dataclasses.asdict(r), ensure_ascii=False)

    if reg is None:
        return CapResult(**json.loads(_run(ctx.task_id, args))), None
    from .registry import Skill
    skill = Skill(name=cap.id, description=cap.description, func=_run,
                  risk_level="low" if cap.risk == "R0" else "medium",
                  timeout_ms=cap.timeout_s * 1000, arg_schema=cap.inputs_schema)
    reg.register(skill)
    res = reg.invoke(ctx.task_id, skill, args)
    if res.status == "denied":
        return CapResult(status="denied", summary=(res.error or "denied")[:200]), res.invocation_id
    if res.status == "timeout":
        return CapResult(status="timeout", summary=(res.error or "timeout")[:200]), res.invocation_id
    try:
        data = json.loads(res.output or "{}")
        return (CapResult(**{k: data.get(k) for k in
                             ("status", "summary", "metrics", "evidence",
                              "diagnostics", "raw_exit", "excerpt")}), res.invocation_id)
    except Exception as e:      # noqa: BLE001
        return CapResult(status="failed", summary=f"结果解析失败：{type(e).__name__}"), res.invocation_id


def build_observation(ctx: TaskCtx, goal: str, mode: str, menu: list[dict],
                      recent_evidence: list[dict], budget: dict,
                      used: dict, repeat_fps: dict | None = None,
                      pre_evidence: list[str] | None = None) -> str:
    """观察包（token 封顶）：目标+任务上下文+菜单+近期证据+剩余预算。
    AL-1（2026-11-26 设计 F-AL1）：证据渲染成"头行+缩进的正文摘录"——模型看得见
    自己取证的内容（检查报告 C1 盲跑根因）；摘录按模式截断（strategy 1500/diagnose 1200）。
    超上限只裁证据正文中段，"## 输出"及之后的指令段永远完整（此前整包硬截会把尾部指令截掉）。
    AL-2：pre_evidence=诊断开工即有的失败摘要（第 0 号证据，单列一节）。"""
    snap = ctx.state_snapshot or {}
    exc_cap = 1500 if mode == "strategy" else 1200

    def _clip_body(text: str, cap: int) -> str:
        text = (text or "").strip()
        if len(text) <= cap:
            return text
        keep = max(cap - 20, 0) // 2
        return text[:keep] + "\n…[中略]…\n" + text[-keep:]

    ev_lines = []
    for e in recent_evidence:
        head = (f"- step{e.get('step')} {e.get('capability')}={e.get('status')}: "
                f"{(e.get('summary') or '')[:120]}")
        exc = _clip_body(e.get("excerpt") or "", exc_cap)
        if exc:
            ev_lines.append(head + "\n    " + exc.replace("\n", "\n    "))
        else:
            ev_lines.append(head)
    pre_lines = []
    for i, p in enumerate(pre_evidence or []):
        pre_lines.append(f"- 摘要[{i}] " + _clip_body(p, exc_cap).replace("\n", "\n  "))

    def _assemble(ev_text: str) -> str:
        obs = (
            f"## 目标\n{goal[:600]}\n\n"
            f"## 任务上下文\n语言={ctx.primary_language} 工作区={ctx.workspace.name} "
            f"轮次={snap.get('round_no')} 冻结={snap.get('verify_mode')} 静态={snap.get('static_only')}\n"
            f"路由范围={snap.get('index_scope_paths') or '全库'}\n"
            + (f"阶段提示：现在是规划阶段，还没有容器，不能运行测试；"
               f"你的任务是通过读代码定位具体风险点。\n" if mode == "strategy" else "")
            + "\n"
            f"## 能力菜单（只能从中选）\n{menu_lines_const}\n\n"
            + (f"## 上一轮失败摘要（第 0 号证据）\n" + "\n".join(pre_lines) + "\n\n"
               if pre_lines else "")
            + f"## 近期证据\n{ev_text}\n\n"
            f"## 剩余预算\n步数 {used.get('steps', 0)}/{budget.get('max_steps')} "
            f"token {used.get('tokens', 0)}/{budget.get('max_tokens')} "
            f"秒 {used.get('seconds', 0)}/{budget.get('max_seconds')}\n\n"
            "## 输出\n只输出一个 JSON 对象（call 或 finish 形态，见系统指令）。"
            + (f"\n注意：这是第 {used.get('steps', 0)} 步（共 {budget.get('max_steps')} 步）"
               "——信息足够时立即 finish，不要继续观察。")
        )
        if (mode == "strategy"
                and used.get("observe_steps", 0) >= int(budget.get("observe_cap")
                                                        or config.AGENT_LOOP_OBSERVE_CAP)):
            obs += ("\n⚠ 观察预算已用尽——本轮必须输出 finish 形态（附 strategy）。"
                    "继续调用观察类能力会被拒绝。")
        if repeat_fps:
            rep = [(fp, n) for fp, n in repeat_fps.items() if n >= 2]
            if rep:
                obs += (f"\n⚠ 已重复的动作（{len(rep)} 个）：其中 "
                        + "、".join(f"{fp[:40]}（第 {n} 次）" for fp, n in rep[:3])
                        + "——重复观察不再提供新信息，请基于已有证据 finish。")
        return obs

    menu_lines_const = "\n".join(f"- {m['id']} ({m['risk']}) {m['description']}\n"
                                 f"  参数: {m.get('args_hint')}" for m in menu)
    CAP = 12000
    ev_text = "\n".join(ev_lines) or "（尚无）"
    obs = _assemble(ev_text)
    if len(obs) > CAP:
        # 只裁证据正文中段（每条摘录再对半砍），指令段/预算段永不动
        bodies = [e for e in ev_lines if "\n    " in e]
        while len(obs) > CAP and bodies:
            bodies.sort(key=len, reverse=True)
            big = bodies.pop(0)
            head, _, body = big.partition("\n    ")
            body = body.replace("\n    ", "\n")
            keep = max(len(body) // 4, 40)
            trimmed = (head + "\n    "
                       + _clip_body(body, keep).replace("\n", "\n    "))
            idx = ev_lines.index(big)
            ev_lines[idx] = trimmed
            bodies.append(trimmed)
            ev_text = "\n".join(ev_lines)
            obs = _assemble(ev_text)
    return obs if len(obs) <= CAP else _assemble("（证据过多已全部省略——请基于已有信息 finish）")


def run_tool_loop(deps, ctx: TaskCtx, goal: str, mode: str, budget: dict,
                  phase: str, loop_no: int = 1,
                  pre_evidence: list[str] | None = None) -> dict:
    """有界决策循环。mode：strategy（规划前观察）| diagnose（失败证据诊断）。
    loop_no：账本 agent_steps 的循环号（UNIQUE(run_id,loop_no,step_no)——同一 run
    多次进循环必须不同号：strategy=1，diagnose 第 N 轮=100+N，见 agent_nodes）。
    pre_evidence：诊断开工即有的失败摘要（AL-2，第 0 号证据——模型不必花步数取证）。
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
    generic_retried = False   # AL-5：泛指策略重试闸（最多一次）
    used = {"steps": 0, "tokens": 0, "seconds": 0}
    t0 = time.monotonic()

    def _stop(reason: str, payload=None) -> dict:
        # prompt_version（任务5）：提示词版本戳随 result 落库——2×2 可比性防线
        # （v0 vs v1 对照必注明，历史数据边界可审计）
        return {"stop": reason, "steps": used["steps"], "used": used,
                "evidence": evidence, "payload": payload or {},
                "prompt_version": AGENT_LOOP_PROMPT_VERSION}

    if not menu:
        return _stop("hard_error", {"summary": "能力菜单为空（配置异常）"})
    try:
        llm = make_chat(phase=phase, temperature=0.2, timeout=120, task_id=ctx.task_id)
    except Exception as e:      # noqa: BLE001 —— 构造失败=hard_error（证据保留）
        log.warning("agent loop make_chat 失败 task=%s: %s", ctx.task_id, e)
        return _stop("hard_error", {"summary": f"LLM 构造失败：{type(e).__name__}"})

    # 任务5 v1（2026-11-22）修断链：此前直用模块常量 AGENT_DECIDE_SYSTEM——
    # build_agent_decide_system(mode) 零调用方，finish 的 strategy/diagnosis 产物键
    # 说明从未注入系统提示词（2×2"策略产物零出现"根因之一，对码实锤）
    system_prompt = build_agent_decide_system(mode)

    max_steps = int(budget.get("max_steps") or 1)
    for step in range(1, max_steps + 1):
        used["steps"] = step
        used["seconds"] = int(time.monotonic() - t0)
        obs = build_observation(ctx, goal, mode, menu, evidence[-3:], budget, used,
                                repeat_fps=seen_action_fps,
                                pre_evidence=pre_evidence)
        t_dec = time.monotonic()
        try:
            resp = llm.invoke([("system", system_prompt), ("user", obs)])
        except Exception as e:  # noqa: BLE001 —— LLM 调用异常（402 分类已在包装层）
            log.warning("agent loop LLM 异常 task=%s: %s", ctx.task_id, e)
            return _stop("hard_error",
                         {"summary": f"LLM 调用异常：{type(e).__name__}: {str(e)[:120]}"})
        used["tokens"] += estimate_tokens(obs) + estimate_tokens(getattr(resp, "content", "") or "")
        llm_call_id = log_llm_call(ctx.task_id, phase, resp,
                                   ctx_budget=getattr(llm, "_ctx_budget", None),
                                   base_url=getattr(llm, "_base_url", ""))
        decision_ms = int((time.monotonic() - t_dec) * 1000)
        raw_text = getattr(resp, "content", "") or ""
        action = _extract_json(raw_text)
        if action is None:
            unparseable += 1
            _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                         action_type="unparseable", decision_json=raw_text,
                         llm_call_id=llm_call_id, decision_ms=decision_ms)
            if unparseable >= 2:
                return _stop("hard_error", {"summary": "模型输出两次不可解析（原始已留档 llm_parse_fail/）"})
            evidence.append({"step": step, "capability": "-", "status": "unparseable",
                             "summary": "输出不可解析，重试一次"})
            continue
        unparseable = 0
        action_json = json.dumps(action, ensure_ascii=False)
        if action.get("action") == "finish":
            # AL-5（2026-11-26 设计 F-AL5）：strategy 泛指策略机器闸——第一次 finish
            # 全是泛指目标不算数，追加证据要求点名具体符号后重试；第二次仍泛指=
            # 接受但产物标 strategy_valid=false（判据 A3 的机器口径）。
            # 仅当 priorities 非空且全泛指时闸（无产物/空优先项=既有"按默认路线走"
            # 路径，不拦——缺产物另有 finish 引导与 S-K1 口径管）。
            if mode == "strategy" and "strategy" in action:
                strat = action.get("strategy") or {}
                if strat.get("priorities") and _strategy_is_generic(strat):
                    if not generic_retried:
                        generic_retried = True
                        _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                                     action_type="finish", decision_json=action_json,
                                     allowed=0,
                                     deny_reason="策略全是泛指目标——要求点名具体函数、文件或边界后重新收尾",
                                     llm_call_id=llm_call_id, decision_ms=decision_ms)
                        evidence.append({"step": step, "capability": "-",
                                         "status": "retry",
                                         "summary": "策略全是泛指目标，请点名具体函数、文件或边界后重新收尾"})
                        continue
                    action["strategy_valid"] = False
                else:
                    action["strategy_valid"] = True
            _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                         action_type="finish", decision_json=action_json,
                         allowed=1, llm_call_id=llm_call_id, decision_ms=decision_ms)
            return _stop("goal_met", payload=action)
        decision = tool_policy.authorize(action, ctx, menu=menu,
                                         seen_fps=seen_action_fps,
                                         budget_used={**used,
                                                      "max_steps": budget.get("max_steps"),
                                                      "max_tokens": budget.get("max_tokens"),
                                                      "max_seconds": budget.get("max_seconds")})
        if decision.stop:
            # 该步已发起（LLM 已决策）但未执行——契约 §1.2 的 not_executed 结局
            _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                         action_type="call", capability=action.get("capability"),
                         args=action.get("args") or {}, decision_json=action_json,
                         allowed=None, deny_reason=f"预算/重复闸停止：{decision.reason}",
                         llm_call_id=llm_call_id, decision_ms=decision_ms,
                         result_status="not_executed")
            return _stop(decision.stop_reason)
        if not decision.allowed:
            _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                         action_type="call", capability=action.get("capability"),
                         args=action.get("args") or {}, decision_json=action_json,
                         allowed=0, deny_reason=decision.reason,
                         llm_call_id=llm_call_id, decision_ms=decision_ms)
            evidence.append({"step": step, "capability": action.get("capability"),
                             "status": "denied", "summary": decision.reason[:200]})
            denied_streak += 1
            if denied_streak >= 3:
                return _stop("policy_deadlock",
                             {"summary": "连续 3 次被拒——策略与权限面失配（OpenHands Stuck 同款判据）"})
            continue
        denied_streak = 0
        cap = find_capability_extended(ctx, action["capability"])
        # v1.1 观察子预算闸（strategy 模式）：observe 类 ≤AGENT_LOOP_OBSERVE_CAP 步，
        # 超出=denied 留痕提示必须 finish（execute 类不受限——预算细化非行为阻止）
        if mode == "strategy" and cap is not None and cap.kind == "observe":
            used["observe_steps"] = used.get("observe_steps", 0)
            ocap = int(budget.get("observe_cap") or config.AGENT_LOOP_OBSERVE_CAP)
            if used["observe_steps"] >= ocap:
                deny_msg = (f"观察子预算耗尽（{used['observe_steps']}/{ocap}）"
                            "——现在必须输出 finish 形态（附 strategy）")
                _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                             action_type="call", capability=cap.id,
                             args=action.get("args") or {}, decision_json=action_json,
                             allowed=0, deny_reason=deny_msg,
                             llm_call_id=llm_call_id, decision_ms=decision_ms)
                evidence.append({"step": step, "capability": cap.id,
                                 "status": "denied",
                                 "summary": deny_msg})
                denied_streak += 1
                if denied_streak >= 3:
                    return _stop("policy_deadlock",
                                 {"summary": "观察子预算耗尽后仍连续尝试观察（无 finish）"})
                continue
            used["observe_steps"] += 1
        afp = action_fingerprint(action["capability"], action["args"])
        seen_action_fps[afp] = seen_action_fps.get(afp, 0) + 1
        cap = find_capability_extended(ctx, action["capability"])
        t_exec = time.monotonic()
        try:
            result, invocation_id = _execute_capability(deps, ctx, cap, action.get("args") or {})
        except Exception as e:  # noqa: BLE001 —— handler 异常降级为 failed 结果
            result = CapResult(status="failed",
                               summary=f"{type(e).__name__}: {str(e)[:150]}")
            invocation_id = None
        exec_ms = int((time.monotonic() - t_exec) * 1000)
        result = normalize_result(result)
        _ledger_step(ctx, mode=mode, loop_no=loop_no, step_no=step,
                     action_type="call", capability=cap.id,
                     args=action.get("args") or {}, decision_json=action_json,
                     allowed=1, llm_call_id=llm_call_id, invocation_id=invocation_id,
                     result_status=result.status, result_summary=result.summary,
                     evidence=result.evidence, decision_ms=decision_ms, exec_ms=exec_ms)
        rfp = _evidence_fp(action["capability"], result)
        if rfp in result_fps:
            new_evidence_streak += 1
            if new_evidence_streak >= 2:
                evidence.append({"step": step, "capability": cap.id,
                                 "status": result.status,
                                 "summary": result.summary,
                                 "excerpt": result.excerpt})
                return _stop("no_new_evidence",
                             {"summary": "连续两次无新证据（结果指纹重复）"})
        else:
            result_fps.add(rfp)
            new_evidence_streak = 0
        evidence.append({"step": step, "capability": cap.id, "status": result.status,
                         "summary": result.summary, "metrics": result.metrics,
                         "evidence": result.evidence, "excerpt": result.excerpt})
    used["seconds"] = int(time.monotonic() - t0)
    return _stop("budget", {"summary": "步数耗尽——已获证据如实保留"})
