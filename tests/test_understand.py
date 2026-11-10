"""需求理解与提炼层（understand）测试矩阵（2026-11-08，用户"任意形态输入"愿景落地）。

单测锁：解析容错（负例→fail-open None）、注入契约（understanding=None 提示词逐字节不变）、
旗标默认关、产物是策略不是条件的口径（render_understanding 只产上下文段）。
节点接线=源码契约断言（nodes.plan 内联调用）。
"""
import json

import pytest

from test_agent import config
from test_agent.skills.prompt_library import build_plan_prompt
from test_agent.skills.understand import (
    render_understanding,
    understand_requirement,
)

UNDERSTANDING = {
    "test_goals": ["验证积分分级的边界行为", "验证加法正确性"],
    "scope_hints": ["Shop.getTier", "Shop.add"],
    "test_approaches": ["边界值分析", "等价类划分"],
    "assumptions": ["任务书中的'交付报告'理解为工程要求，非被测行为"],
}


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """理解层单测默认开旗标（旗标关契约单独测）。"""
    monkeypatch.setattr(config, "UNDERSTAND_ENABLED", True)


class FakeResp:
    def __init__(self, content):
        self.content = content


class FakeChat:
    def __init__(self, content, **kw):
        self.content = content

    def invoke(self, msgs):
        return FakeResp(self.content)


# —— 解析层（正例/容错/fail-open） ——

def test_understand_parses_llm_json():
    payload = json.dumps(UNDERSTANDING, ensure_ascii=False)
    out = understand_requirement("k", "测一下分级和加法", llm_factory=lambda **kw: FakeChat(payload))
    assert out and out["test_goals"] == UNDERSTANDING["test_goals"]
    assert out["assumptions"] == UNDERSTANDING["assumptions"]


def test_understand_strips_code_fence():
    payload = "```json\n" + json.dumps(UNDERSTANDING, ensure_ascii=False) + "\n```"
    out = understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat(payload))
    assert out and len(out["scope_hints"]) == 2


def test_understand_unparseable_fail_open():
    out = understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat("没有 JSON 的输出"))
    assert out is None


def test_understand_empty_goals_fail_open():
    # 没提炼出任何方向 = 理解失败（不硬造）
    payload = json.dumps({"test_goals": [], "assumptions": ["x"]}, ensure_ascii=False)
    out = understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat(payload))
    assert out is None


def test_understand_empty_or_disabled():
    assert understand_requirement("k", "") is None
    assert understand_requirement("k", None) is None


def test_understand_flag_off_short_circuit(monkeypatch):
    monkeypatch.setattr(config, "UNDERSTAND_ENABLED", False)
    called = []
    out = understand_requirement("k", "测分级", llm_factory=lambda **kw: FakeChat("{}") or called.append(1))
    assert out is None and not called     # 旗标关=零 LLM 调用


def test_understand_lists_and_scalars_normalized():
    payload = json.dumps({"test_goals": "单条方向", "scope_hints": ["a", "", None],
                          "test_approaches": 3}, ensure_ascii=False)
    out = understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat(payload))
    assert out["test_goals"] == ["单条方向"]
    assert out["scope_hints"] == ["a"]
    assert out["test_approaches"] == ["3"]


def test_understand_on_resp_audit_callback():
    seen = []
    payload = json.dumps(UNDERSTANDING, ensure_ascii=False)
    understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat(payload),
                           on_resp=lambda resp: seen.append(resp.content))
    assert seen and "边界行为" in seen[0]


def test_understand_on_resp_failure_does_not_break():
    payload = json.dumps(UNDERSTANDING, ensure_ascii=False)
    def boom(resp):
        raise RuntimeError("审计挂了")
    out = understand_requirement("k", "x", llm_factory=lambda **kw: FakeChat(payload), on_resp=boom)
    assert out is not None               # 审计失败不拦主链路


# —— 注入契约（understanding=None 提示词逐字节不变；有产物才注入） ——

def test_plan_prompt_unchanged_without_understanding():
    a = build_plan_prompt("需求文本")
    b = build_plan_prompt("需求文本", understanding=None)
    assert a == b                        # 默认关口径保护
    assert "需求理解" not in a


def test_plan_prompt_injects_understanding():
    text = build_plan_prompt("需求文本", understanding=UNDERSTANDING)
    assert "需求理解（理解层提炼" in text
    assert "边界行为" in text and "显式假设" in text
    assert "工程要求，非被测行为" in text


def test_render_understanding_empty_honest():
    assert render_understanding(None) == ""
    assert render_understanding({"test_goals": [], "scope_hints": [],
                                 "test_approaches": [], "assumptions": []}) == ""


# —— 节点接线契约（源码断言：plan 内联调用+审计回调） ——

def test_plan_node_wires_understanding():
    import inspect
    from test_agent.graph import nodes
    src = inspect.getsource(nodes.make_nodes)
    assert "understand_requirement(" in src
    assert "log_llm_call(tid, \"understand\", resp," in src   # token 记账完整性（2026-11-10 起带 ctx_budget 对账参数）
    assert "understanding=understanding" in src               # 注入 plan 提示词
    assert "[understand] 理解层未产出（fail-open 透传原文拆解）" in src


def test_flag_default_off_contract():
    # 默认关=消融纪律（开/关对比输入形态鲁棒性），防静默漂移——源码级默认值锁定
    from pathlib import Path
    src = Path(config.__file__).read_text(encoding="utf-8")
    assert '_flag("MTA_UNDERSTAND", False)' in src
