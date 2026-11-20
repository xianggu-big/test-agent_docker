"""包 1 决策循环测试（2026-11-20 设计稿 §3.3/§3.8 表 8 例）。

fake LLM（脚本化 action 序列）+ tmp 工作区——零容器（只触发 read 类 handler）、
零 LLM 调用（make_chat monkeypatch）、零真图。覆盖：步数耗尽/goal_met/两次不可解析
fail-closed/连续无新证据/拒绝留痕+policy_deadlock/finish 透传/异常 stop/拒绝后恢复。
"""
import json
import types

import pytest

from test_agent.skills import tool_loop
from test_agent.skills.capabilities import CapResult, TaskCtx


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
