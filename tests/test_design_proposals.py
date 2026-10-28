"""三方案改造的机制单测（fake LLM 注入，不触网）：
A 断言质量审计+闸（apply_assertion_gate）/ B PBT 骨架（提示词开关）/ C 条件缺口回灌（refill_condition_gaps）。
"""
import json
from dataclasses import dataclass
from pathlib import Path

from test_agent.skills.gen_tests import (_condition_gaps, apply_assertion_gate,
                                         assertion_audit, refill_condition_gaps)
from test_agent.skills.prompt_library import build_user_prompt


# ---------- 方案A：断言质量审计 ----------

def test_audit_strong_value_comparison():
    a = assertion_audit("def test_x():\n    assert add(1, 2) == 3\n")
    assert a["strong"] == 1 and a["weak"] == 0


def test_audit_weak_bare_truthiness_and_is_not_none():
    a = assertion_audit("def test_x():\n"
                        "    r = add(1, 2)\n"
                        "    assert r\n"
                        "    assert r is not None\n")
    assert a["strong"] == 0 and a["weak"] == 2


def test_audit_no_assert_counts_weak():
    a = assertion_audit("def test_x():\n    add(1, 2)\n")
    assert a["strong"] == 0 and a["weak"] == 1 and "无断言" in a["reasons"][0]


def test_audit_raises_context_manager_is_strong():
    a = assertion_audit("def test_x():\n"
                        "    with pytest.raises(ValueError):\n"
                        "        parse(-1)\n")
    assert a["strong"] == 1 and a["weak"] == 0


def test_audit_is_true_is_strong_is_none_is_weak():
    a = assertion_audit("def test_x():\n    assert check('') is True\n")
    b = assertion_audit("def test_x():\n    assert check('') is not None\n")
    assert a["strong"] == 1
    assert b["strong"] == 0 and b["weak"] == 1


def test_audit_boolop_joined_asserts_audit_per_arm():
    """module_check FINDING：两臂皆具体比较的联接断言不再整体误判弱。"""
    a = assertion_audit("def test_x():\n    assert a == 1 and b == 2\n")
    assert a["strong"] == 1 and a["weak"] == 0
    # 嵌套联接：内层两臂强、外层与具体比较联接 → 整体强
    b = assertion_audit("def test_x():\n    assert (a == 1 and b == 2) and c == 3\n")
    assert b["strong"] == 1 and b["weak"] == 0
    # 含弱臂（裸真值）→ 整体弱，reason 指明第几臂
    c = assertion_audit("def test_x():\n    assert a == 1 and flag\n")
    assert c["strong"] == 0 and c["weak"] == 1 and "第2臂" in c["reasons"][0]


def test_gate_keeps_mixed_drops_all_weak():
    cases = [{"name": "t_strong", "code": "def t_strong():\n    assert f() == 1\n"},
             {"name": "t_weak", "code": "def t_weak():\n    assert f()\n"}]
    kept, rejected, stats = apply_assertion_gate(cases, gate=True)
    assert [c["name"] for c in kept] == ["t_strong"]
    assert len(rejected) == 1 and "t_weak" in rejected[0]
    assert stats["cases"] == 2 and stats["weak_cases"] == 1 and stats["gate_rejected"] == 1


def test_gate_off_computes_stats_without_filtering():
    cases = [{"name": "t_strong", "code": "def t_strong():\n    assert f() == 1\n"},
             {"name": "t_weak", "code": "def t_weak():\n    assert f()\n"}]
    kept, rejected, stats = apply_assertion_gate(cases, gate=False)
    assert len(kept) == 2 and rejected == []
    assert stats["weak_cases"] == 1 and "gate_rejected" not in stats


# ---------- 方案B：PBT 骨架注入 ----------

def test_pbt_section_toggle():
    src = [("m.py", "def f(x):\n    return x\n")]
    on = build_user_prompt(src, "需求", pbt=True)
    assert "属性测试" in on and "hypothesis" in on and "@given" in on
    assert "属性测试（第七法" not in build_user_prompt(src, "需求", pbt=False)


# ---------- 方案C：条件缺口回灌 ----------

@dataclass
class _Resp:
    content: str


class _FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, messages):
        m = messages[1]   # gen_tests 传的是 ("user", prompt) 元组（真实 ChatOpenAI 亦兼容）
        self.prompts.append(m.content if hasattr(m, "content") else m[1])
        return _Resp(self.payload)


def _gap_fixture():
    conds = [{"condition": "空密码应判为无效"},
             {"condition": "长度恰为上限的密码应有效"}]
    cases = [{"name": "test_empty_边界值分析", "expected": "无效",
              "code": "def test_empty_边界值分析():\n"
                      "    assert check('') is False  # 空密码 无效\n"}]
    return conds, cases


def test_condition_gaps_finds_uncovered_by_2gram():
    conds, cases = _gap_fixture()
    gaps = _condition_gaps(conds, cases)
    assert len(gaps) == 1 and "上限" in gaps[0]["condition"]


def test_refill_targets_gap_with_forced_prompt_and_merges(tmp_path: Path):
    conds, cases = _gap_fixture()
    payload = json.dumps({"cases": [{"name": "test_max_boundary_边界值分析",
                                     "code": "def test_max_boundary_边界值分析():\n"
                                             "    assert check('xxxxxxxxxx') is True\n"}]})
    llm = _FakeLLM(payload)
    new, imports, info = refill_condition_gaps(
        llm, [("m.py", "def check(p):\n    return bool(p)\n")], "需求",
        conds, cases, tmp_path, set(), True, False)
    assert info["uncovered_before"] == 1 and info["refill_generated"] == 1
    assert info["uncovered_after"] == 1     # 2-gram 仍映射不上（用例名与条件文本无交集）——如实计数
    assert new[0]["name"] == "test_max_boundary_边界值分析"
    assert "强制逐条覆盖" in llm.prompts[0]
    assert "test_empty_边界值分析" in llm.prompts[0]   # 已有用例名单进提示词防重复


def test_refill_no_gap_makes_no_llm_call(tmp_path: Path):
    conds = [{"condition": "空密码应判为无效"}]
    cases = [{"name": "t", "expected": "无效",
              "code": "def t():\n    assert check('') is False  # 空密码 无效\n"}]
    llm = _FakeLLM("{}")
    new, imports, info = refill_condition_gaps(
        llm, [("m.py", "x = 1")], "", conds, cases, tmp_path, set(), True, False)
    assert new == [] and info["refill_generated"] == 0 and not llm.prompts


def test_refill_unparsable_output_degrades_honestly(tmp_path: Path):
    conds, cases = _gap_fixture()
    llm = _FakeLLM("这不是 JSON")
    new, imports, info = refill_condition_gaps(
        llm, [("m.py", "x = 1")], "", conds, cases, tmp_path, set(), True, False)
    assert new == [] and "不可解析" in info.get("note", "")
