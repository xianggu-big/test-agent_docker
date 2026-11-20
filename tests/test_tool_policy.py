"""包 1 授权层测试（2026-11-20 设计稿 §3.2 七闸；§3.8 表 10 例）。

七闸顺序=拒绝优先级：协议→存在→schema→密钥→模式矩阵→预算→重复。
铁律：零容器、零 LLM。"""
import pytest

from test_agent.skills import tool_policy
from test_agent.skills.capabilities import TaskCtx
from test_agent.skills.tool_policy import authorize


def _ctx(tmp_path, **snap):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return TaskCtx(task_id=1, session_id=1, workspace=ws, container_name="c",
                   primary_language="python", image="img", state_snapshot=snap,
                   conn=None, runner=None, artifacts_dir=tmp_path / "artifacts")


def test_protocol_gate(tmp_path):
    ctx = _ctx(tmp_path)
    for bad in (None, {"action": "jump"}, {"action": "call", "capability": "x"}):
        d = authorize(bad, ctx)
        assert not d.allowed and not d.stop


def test_existence_gate(tmp_path):
    ctx = _ctx(tmp_path)
    d = authorize({"action": "call", "capability": "test.nonexistent", "args": {}}, ctx)
    assert not d.allowed and "不在菜单" in d.reason


def test_schema_gate_rejects(tmp_path):
    ctx = _ctx(tmp_path)
    d = authorize({"action": "call", "capability": "test.observe.read",
                   "args": {"path": "a.py"}}, ctx)          # 缺 required mode
    assert not d.allowed and "mode" in d.reason
    d2 = authorize({"action": "call", "capability": "test.observe.read",
                    "args": {"path": "a.py", "mode": "read", "evil": 1}}, ctx)
    assert not d2.allowed and "evil" in d2.reason            # 未知键拒


def test_secret_key_gate(tmp_path):
    """0-D：args 带密钥键名 → denied（黑名单正则）。"""
    ctx = _ctx(tmp_path)
    d = authorize({"action": "call", "capability": "test.run.unit",
                   "args": {"targets": ["t"], "api_key": "sk-123"}}, ctx)
    assert not d.allowed and "api_key" in d.reason


def test_verify_mode_r0_only(tmp_path):
    """模式矩阵：verify 冻结任务 R1 全拒（gen.cases/edit.scaffold）+ R0 放行。"""
    ctx = _ctx(tmp_path, verify_mode=1)
    d = authorize({"action": "call", "capability": "test.gen.cases",
                   "args": {"feedback": "x"}}, ctx)
    assert not d.allowed and "验证冻结" in d.reason
    d2 = authorize({"action": "call", "capability": "test.observe.read",
                    "args": {"path": "a.py", "mode": "read"}}, ctx)
    assert d2.allowed and d2.risk == "R0"


def test_static_mode_r0_only(tmp_path):
    ctx = _ctx(tmp_path, static_only=True)
    d = authorize({"action": "call", "capability": "test.gen.cases",
                   "args": {"feedback": "x"}}, ctx)   # R1 → static 模式拒绝
    assert not d.allowed and "静态模式" in d.reason
    d2 = authorize({"action": "call", "capability": "test.observe.read",
                    "args": {"path": "a.py", "mode": "read"}}, ctx)  # R0 → 放行
    assert d2.allowed


def test_normal_mode_r1_allowed_r2_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    d = authorize({"action": "call", "capability": "test.gen.cases",
                   "args": {"feedback": "补一条"}}, ctx)
    assert d.allowed and d.risk == "R1"
    # MVP 菜单无 R2+ 能力——构造越级请求经存在闸拒绝（R2 归包 2）
    d2 = authorize({"action": "call", "capability": "plugin.install", "args": {}}, ctx)
    assert not d2.allowed


def test_budget_gate_stops(tmp_path):
    ctx = _ctx(tmp_path)
    d = authorize({"action": "call", "capability": "test.observe.read",
                   "args": {"path": "a.py", "mode": "read"}},
                  ctx, budget_used={"steps": 8, "max_steps": 8})
    assert d.stop and d.stop_reason == "budget"


def test_repeat_gate_stops(tmp_path):
    ctx = _ctx(tmp_path)
    act = {"action": "call", "capability": "test.observe.read",
           "args": {"path": "a.py", "mode": "read"}}
    seen = {}
    for _ in range(2):
        seen[tool_policy.action_fingerprint(act["capability"], act["args"])] = \
            seen.get(tool_policy.action_fingerprint(act["capability"], act["args"]), 0) + 1
        d = authorize(act, ctx, seen_fps=seen)
        assert d.allowed
    d3 = authorize(act, ctx, seen_fps=seen)
    assert d3.stop and d3.stop_reason == "repeated_action"


def test_finish_passthrough(tmp_path):
    ctx = _ctx(tmp_path)
    d = authorize({"action": "finish", "summary": "done"}, ctx)
    assert d.allowed and not d.stop
