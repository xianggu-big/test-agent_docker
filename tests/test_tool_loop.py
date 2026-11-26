"""包 1 决策循环测试（2026-11-20 设计稿 §3.3/§3.8 表 8 例）。

fake LLM（脚本化 action 序列）+ tmp 工作区——零容器（只触发 read 类 handler）、
零 LLM 调用（make_chat monkeypatch）、零真图。覆盖：步数耗尽/goal_met/两次不可解析
fail-closed/连续无新证据/拒绝留痕+policy_deadlock/finish 透传/异常 stop/拒绝后恢复。
"""
import json
import types

import pytest

from test_agent.skills import capabilities as caps
from test_agent.skills import tool_loop
from test_agent.skills.capabilities import CapResult, TaskCtx
from test_agent.skills.prompt_library import (AGENT_STRATEGY_FINISH_EXAMPLE,
                                              build_agent_decide_system)
from test_agent.skills.registry import _validate_args


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if isinstance(self.script[0], Exception):
            raise self.script.pop(0)
        return _FakeResp(self.script.pop(0))


def _ctx(tmp_path, **snap):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    f = ws / "a.py"
    f.write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    return TaskCtx(task_id=7, session_id=1, workspace=ws, container_name="c",
                   primary_language="python", image="img", state_snapshot=snap,
                   conn=None, runner=None, artifacts_dir=tmp_path / "artifacts")


def _deps():
    return types.SimpleNamespace(registry=None)   # 纯单测形态：直接调 handler（零审计）


def _run(tmp_path, script, *, budget=None, snap=None, monkeypatch):
    ctx = _ctx(tmp_path, **(snap or {}))
    fake = _FakeLLM(script)
    monkeypatch.setattr(tool_loop, "make_chat", lambda **kw: fake)
    monkeypatch.setattr(tool_loop, "log_llm_call", lambda *a, **k: None)
    b = budget or {"max_steps": 4, "max_tokens": 30000, "max_seconds": 600}
    result = tool_loop.run_tool_loop(_deps(), ctx, "目标：读文件定位", "strategy",
                                     b, "agent_strategy")
    return result, fake


def test_budget_exhaustion(tmp_path, monkeypatch):
    """步数耗尽：call 循环跑满 max_steps → stop=budget，证据如实保留。"""
    act = json.dumps({"action": "call", "capability": "test.observe.read",
                      "args": {"path": "a.py", "mode": "read"}})
    result, _ = _run(tmp_path, [act] * 5, budget={"max_steps": 3, "max_tokens": 99999,
                                                  "max_seconds": 9999}, monkeypatch=monkeypatch)
    assert result["stop"] == "budget" and result["steps"] == 3
    # 第 3 步进 authorize 即触预算顶（闸序=预算在重复之前）——实际执行 2 步留 2 条证据
    assert len(result["evidence"]) == 2
    assert result["evidence"][0]["capability"] == "test.observe.read"


def test_goal_met_with_payload(tmp_path, monkeypatch):
    """finish → goal_met + payload 透传（strategy 键可提取）。"""
    script = [json.dumps({"action": "call", "capability": "test.observe.read",
                          "args": {"path": "a.py", "mode": "read"}}),
              json.dumps({"action": "finish", "summary": "读完",
                          "strategy": {"priorities": [{"order": 1,
                                                       "capability_id": "test.run.unit",
                                                       "why": "先跑"}],
                                       "evidence_plan": ["run_stdout"],
                                       "fallback": "回退全库"}})]
    result, _ = _run(tmp_path, script, monkeypatch=monkeypatch)
    assert result["stop"] == "goal_met"
    assert result["payload"]["strategy"]["priorities"][0]["capability_id"] == "test.run.unit"


def test_unparseable_twice_fail_closed(tmp_path, monkeypatch):
    """两次不可解析 → hard_error（fail-closed，不猜语义）。"""
    result, _ = _run(tmp_path, ["这不是JSON", "也不是{JSON"], monkeypatch=monkeypatch)
    assert result["stop"] == "hard_error"
    assert any(e["status"] == "unparseable" for e in result["evidence"])


def test_unparseable_retry_then_recover(tmp_path, monkeypatch):
    """一次不可解析重试后正常 → 不停（宽容解析+重试 1 次的契约）。"""
    script = ["垃圾输出",
              json.dumps({"action": "finish", "summary": "恢复"})]
    result, _ = _run(tmp_path, script, monkeypatch=monkeypatch)
    assert result["stop"] == "goal_met"


def test_no_new_evidence_stops(tmp_path, monkeypatch):
    """连续两次结果指纹相同（不同文件同内容）→ no_new_evidence。"""
    ctx_ws = tmp_path / "ws"
    ctx_ws.mkdir(parents=True, exist_ok=True)
    (ctx_ws / "b.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (ctx_ws / "c.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    script = [json.dumps({"action": "call", "capability": "test.observe.read",
                          "args": {"path": "a.py", "mode": "read"}}),
              json.dumps({"action": "call", "capability": "test.observe.read",
                          "args": {"path": "b.py", "mode": "read"}}),
              json.dumps({"action": "call", "capability": "test.observe.read",
                          "args": {"path": "c.py", "mode": "read"}})]
    result, _ = _run(tmp_path, script, monkeypatch=monkeypatch)
    assert result["stop"] == "no_new_evidence"


def test_denied_streak_policy_deadlock(tmp_path, monkeypatch):
    """verify 冻结下连续 3 次 R1 请求 → 3 denied → policy_deadlock（调研修正 3）。"""
    act = json.dumps({"action": "call", "capability": "test.gen.cases",
                      "args": {"feedback": "补"}})
    result, _ = _run(tmp_path, [act] * 4, snap={"verify_mode": 1}, monkeypatch=monkeypatch)
    assert result["stop"] == "policy_deadlock"
    denied = [e for e in result["evidence"] if e["status"] == "denied"]
    assert len(denied) == 3, "拒绝必须逐条留痕"


def test_denied_then_recover(tmp_path, monkeypatch):
    """拒绝后换目标成功 → 不停（换目标而不是换姿势的正确行为）。"""
    script = [json.dumps({"action": "call", "capability": "test.gen.cases",
                          "args": {"feedback": "补"}}),                # denied（冻结）
              json.dumps({"action": "finish", "summary": "改读证据"})]
    result, _ = _run(tmp_path, script, snap={"verify_mode": 1}, monkeypatch=monkeypatch)
    assert result["stop"] == "goal_met"


def test_llm_exception_hard_error(tmp_path, monkeypatch):
    """LLM 调用异常 → hard_error（fail-closed，绝不部分重试）。"""
    result, _ = _run(tmp_path, [RuntimeError("402 boom")], monkeypatch=monkeypatch)
    assert result["stop"] == "hard_error"
    assert "402" in (result["payload"] or {}).get("summary", "")

# ---------- v1.1 观察子预算（finish 引导，2026-11-21 欠账清偿） ----------

def test_observe_cap_forces_finish_path(tmp_path, monkeypatch):
    """strategy 模式观察子预算（默认 3）：第 4 次 observe 类调用被拒（denied 留痕
    含"观察子预算"），随后 finish → goal_met。2×2 实锤模型倾向观察不 finish 的闸层引导。"""
    from test_agent import config
    monkeypatch.setattr(config, "AGENT_LOOP_OBSERVE_CAP", 3)
    # 变化参数（不同 offset）——完全相同重复走重复闸（更早），观察闸管"变化但同 kind"
    reads = [json.dumps({"action": "call", "capability": "test.observe.read",
                         "args": {"path": "a.py", "mode": "read", "offset": i}})
             for i in range(1, 5)]
    script = reads + [json.dumps({"action": "finish", "summary": "观察够了",
                                  "strategy": {"priorities": [{"order": 1,
                                                               "capability_id": "test.run.unit",
                                                               "why": "跑"}]}})]
    result, _ = _run(tmp_path, script, budget={"max_steps": 6, "max_tokens": 99999,
                                               "max_seconds": 9999}, monkeypatch=monkeypatch)
    assert result["stop"] == "goal_met", f"实得 {result['stop']}"
    denied = [e for e in result["evidence"] if e["status"] == "denied"]
    assert denied and "观察子预算" in denied[0]["summary"], "第 4 次 observe 必须被子预算闸拒"
    assert result["payload"]["strategy"]["priorities"], "finish 产物 strategy 必须透传"


def test_observe_cap_zero_denies_first_observe(tmp_path, monkeypatch):
    from test_agent import config
    monkeypatch.setattr(config, "AGENT_LOOP_OBSERVE_CAP", 0)
    act = json.dumps({"action": "call", "capability": "test.observe.read",
                      "args": {"path": "a.py", "mode": "read"}})
    script = [act, json.dumps({"action": "finish", "summary": "直接给结论",
                               "strategy": {"priorities": []}})]
    result, _ = _run(tmp_path, script, budget={"max_steps": 4, "max_tokens": 99999,
                                               "max_seconds": 9999}, monkeypatch=monkeypatch)
    assert result["stop"] == "goal_met"
    assert result["evidence"][0]["status"] == "denied"


def test_observe_cap_not_for_execute(tmp_path, monkeypatch):
    """execute 类不受观察子预算限（run.unit 第 4 步仍放行——子预算只管 observe kind）。"""
    from test_agent import config
    monkeypatch.setattr(config, "AGENT_LOOP_OBSERVE_CAP", 3)
    monkeypatch.setattr(tool_loop, "_execute_capability", lambda *a, **k: CapResult(
        status="ok", summary="run ok", metrics={"exit_code": 0},
        evidence=[{"kind": "x", "path": "p", "sha256": "h"*64}]))
    script = [json.dumps({"action": "call", "capability": "test.run.unit",
                          "args": {"targets": ["a.py"]}}) for _ in range(4)]
    script.append(json.dumps({"action": "finish", "summary": "ok",
                              "strategy": {"priorities": []}}))
    result, _ = _run(tmp_path, script, budget={"max_steps": 6, "max_tokens": 99999,
                                               "max_seconds": 9999}, monkeypatch=monkeypatch)
    denied = [e for e in result["evidence"] if e["status"] == "denied"]
    assert not denied, "execute 类不被观察子预算拒绝"


def test_observation_warns_when_observe_cap_exhausted(tmp_path, monkeypatch):
    """观察包在子预算耗尽后注入 finish 强提醒（信息提供层）。"""
    from test_agent.skills.capabilities import TaskCtx
    ctx = _ctx(tmp_path)
    budget = {"max_steps": 8, "max_tokens": 30000, "max_seconds": 600}
    used = {"steps": 4, "observe_steps": 3, "tokens": 0, "seconds": 0}
    obs = tool_loop.build_observation(ctx, "目标", "strategy",
                                      [{"id": "test.observe.read", "description": "d",
                                        "risk": "R0", "args_hint": ""}],
                                      [], budget, used)
    assert "观察预算已用尽" in obs and "finish" in obs


# ---------- 任务5 v1（2026-11-22）：finish 引导——断链修复+三问+示例+重复计数+版本戳 ----------

def test_prompt_version_v1():
    """版本戳常量（2×2 可比性防线：v0/v1/v2 对照必注明）。"""
    from test_agent.skills.prompt_library import AGENT_LOOP_PROMPT_VERSION
    assert AGENT_LOOP_PROMPT_VERSION == "v2"


# ---------- Q4（2026-11-26）：示例机器可核对——示例 ⊆ 菜单、示例参数 ⊆ schema ----------

def _menu_schemas():
    """真实菜单 {id: inputs_schema}——单一真源是 capabilities，不手抄。"""
    ctx = types.SimpleNamespace(primary_language="python", agent_mode="strategy")
    return {c.id: c.inputs_schema for c in caps._build_capabilities(ctx)}


def _example_priorities():
    """从 finish 示例常量里取出 priorities。对常量本身解析，不对系统提示词全文——
    全文里 '{"action": "finish"' 出现两次（模板行含 {mode_extra_key} 占位，不是合法 JSON）。"""
    idx = AGENT_STRATEGY_FINISH_EXAMPLE.index('{"action": "finish"')
    return json.loads(AGENT_STRATEGY_FINISH_EXAMPLE[idx:])["strategy"]["priorities"]


def test_strategy_finish_example_injected_verbatim():
    """示例原样注入 strategy 系统提示词，且不混入 diagnose（防断链、防模式串味）。"""
    assert AGENT_STRATEGY_FINISH_EXAMPLE in build_agent_decide_system("strategy")
    assert AGENT_STRATEGY_FINISH_EXAMPLE not in build_agent_decide_system("diagnose")


def test_strategy_finish_example_ids_exist_in_menu():
    """示例里的每个 capability_id 都是真实菜单 id（Q4 起因：示例写了简写 gen_cases）。"""
    schemas = _menu_schemas()
    for p in _example_priorities():
        assert p["capability_id"] in schemas, f"示例能力 id 不在菜单：{p['capability_id']}"


def test_strategy_finish_example_args_hints_pass_schema():
    """示例里每个 args_hint 都能过对应能力的 schema 校验——用策略闸同一个校验器。"""
    schemas = _menu_schemas()
    for p in _example_priorities():
        schema = schemas.get(p["capability_id"])
        assert schema is not None, f"示例能力 id 不在菜单：{p['capability_id']}"
        err = _validate_args(schema, p["args_hint"])
        assert err is None, f"{p['capability_id']} 的示例参数不过 schema：{err}"


def test_decide_system_strategy_has_finish_key_and_example():
    """断链修复验收：strategy 模式系统提示词必须含 finish 产物键说明（priorities）
    + 最小 finish 示例 + 三问判定锚（此前 build_agent_decide_system 零调用方
    =从未注入，策略产物零出现的根因之一）。"""
    from test_agent.skills.prompt_library import build_agent_decide_system
    s = build_agent_decide_system("strategy")
    assert '"strategy"' in s and "priorities" in s, "finish 产物键说明必须在"
    assert '"action": "finish"' in s and "evidence_plan" in s, "finish 示例必须在"
    assert "再观察一次会改变结论吗" in s, "三问判定锚必须在"


def test_decide_system_diagnose_has_discipline_not_strategy_example():
    """diagnose 模式：DISCIPLINE 段在，strategy 示例不在（模式互斥）。"""
    from test_agent.skills.prompt_library import build_agent_decide_system
    s = build_agent_decide_system("diagnose")
    assert "分类定义" in s, "诊断纪律段必须在"
    assert "evidence_plan" not in s, "strategy 示例不得混入 diagnose"


def test_observation_shows_repeat_counts(tmp_path):
    """重复观察惩罚进 obs：重复指纹的计数可见（第 N 次）。"""
    ctx = _ctx(tmp_path)
    budget = {"max_steps": 8, "max_tokens": 30000, "max_seconds": 600}
    used = {"steps": 3, "observe_steps": 2, "tokens": 0, "seconds": 0}
    obs = tool_loop.build_observation(
        ctx, "目标", "strategy",
        [{"id": "test.observe.read", "description": "d", "risk": "R0", "args_hint": ""}],
        [], budget, used,
        repeat_fps={"read|src/a.py|200": 3})
    assert "第 3 次" in obs and "重复观察不再提供新信息" in obs


def test_observation_no_repeat_section_when_none(tmp_path):
    """无重复指纹（空 dict / 默认 None）=零新增文案（不无条件加段）。"""
    ctx = _ctx(tmp_path)
    budget = {"max_steps": 8, "max_tokens": 30000, "max_seconds": 600}
    used = {"steps": 2, "observe_steps": 1, "tokens": 0, "seconds": 0}
    menu = [{"id": "test.observe.read", "description": "d", "risk": "R0", "args_hint": ""}]
    obs = tool_loop.build_observation(ctx, "目标", "strategy", menu, [], budget, used,
                                      repeat_fps={"x": 1})
    assert "已重复的动作" not in obs, "只见 1 次的指纹不算重复"
    obs2 = tool_loop.build_observation(ctx, "目标", "strategy", menu, [], budget, used)
    assert "已重复的动作" not in obs2, "默认参数（None）也不出现重复段"
