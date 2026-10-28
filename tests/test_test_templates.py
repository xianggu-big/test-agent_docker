"""槽位填充模板库（test_templates.py）与双通道生成协议的机制单测。"""
import ast

import pytest

from test_agent.skills import gen_tests
from test_agent.skills.test_templates import (
    KIND_METHOD,
    SlotError,
    expand_template_cases,
    render_slot_template,
)

T1_SLOTS = {
    "mode": "valid",
    "imports": "from calc import div",
    "target": "div({v}, 2)",
    "cases": [
        {"value": 10, "expected": 5, "label": "有效类·正偶数"},
        {"value": -4, "expected": -2, "label": "有效类·负数"},
    ],
}


def _render(kind, slots, name="test_div"):
    code = render_slot_template(kind, slots, base_name=name)
    ast.parse(code)                     # 渲染产物必须语法合法
    return code


def test_t1_valid_render():
    code = _render("T1", T1_SLOTS)
    assert "import pytest" in code
    assert "from calc import div" in code
    assert '@pytest.mark.parametrize("value,expected"' in code
    assert "def test_test_div_等价类(value, expected):" in code
    assert "div(value, 2) == expected" in code
    assert "(10, 5)" in code and "(-4, -2)" in code
    assert "有效类·正偶数" in code        # ids 保留语义标签


def test_t1_invalid_mode_renders_raises():
    slots = {"mode": "invalid", "imports": "from calc import div",
             "target": "div({v}, 0)",
             "cases": [{"value": 1, "error": "ZeroDivisionError", "label": "除零"},
                       {"value": "x", "error": "TypeError", "label": "类型错"}]}
    code = _render("T1", slots)
    assert '@pytest.mark.parametrize("value,error"' in code
    assert "with pytest.raises(error):" in code
    assert "def test_test_div_等价类_无效输入(value, error):" in code


def test_t2_requires_four_boundary_points():
    slots = {**T1_SLOTS, "cases": T1_SLOTS["cases"][:2]}
    with pytest.raises(SlotError, match="边界点不足"):
        render_slot_template("T2", slots, base_name="x")
    points = [{"value": v, "expected": 0, "label": l} for v, l in
              [(1, "min-1"), (2, "min"), (3, "min+1"), (9, "max-1"), (10, "max"), (11, "max+1")]]
    code = _render("T2", {**T1_SLOTS, "cases": points})
    assert "def test_test_div_边界值(" in code


def test_t5_min_two_attack_cases():
    slots = {"mode": "invalid", "imports": "from calc import div", "target": "div({v}, 1)",
             "cases": [{"value": None, "error": "TypeError", "label": "空值"},
                       {"value": 0, "error": "ZeroDivisionError", "label": "零值"}]}
    code = _render("T5", slots)
    assert "def test_test_div_错误推测_无效输入(" in code


def test_slot_validation_errors():
    with pytest.raises(SlotError, match="未知模板"):
        render_slot_template("T9", T1_SLOTS)
    with pytest.raises(SlotError, match="target"):
        render_slot_template("T1", {**T1_SLOTS, "target": "div(v, 2)"})
    with pytest.raises(SlotError, match="内置异常"):
        render_slot_template("T1", {**T1_SLOTS, "mode": "invalid",
                                    "cases": [{"value": 1, "error": "MyCustomError"}]})
    with pytest.raises(SlotError, match="expected"):
        render_slot_template("T1", {**T1_SLOTS,
                                    "cases": [{"value": 1}]})
    with pytest.raises(SlotError, match="imports"):
        render_slot_template("T1", {**T1_SLOTS, "imports": "  "})


def test_expand_template_cases_mixed_channels():
    """双通道：模板案展开、自由代码案原样、坏槽位案剔除并留理由。"""
    free = {"name": "test_free", "method": "判定表",
            "code": "def test_free():\n    assert 1 == 1\n"}
    good = {"name": "test_div_eq", "template": "T1", "slots": T1_SLOTS}
    bad = {"name": "test_bad", "template": "T2",
           "slots": {**T1_SLOTS, "cases": T1_SLOTS["cases"][:1]}}
    out, rejected = expand_template_cases([free, good, bad])

    assert [c["name"] for c in out] == ["test_free", "test_div_eq"]
    assert out[0]["code"] == free["code"]              # 自由通道不被改动
    assert out[1]["method"] == KIND_METHOD["T1"]       # 模板案自动标注方法
    assert "def test_test_div_eq_等价类(" in out[1]["code"]
    assert len(rejected) == 1 and "槽位无效" in rejected[0] and "test_bad" in rejected[0]


def test_parse_cases_accepts_template_channel():
    """解析层收两种形态：自由 code + 槽位模板（无 code 字段）。"""
    raw = """{"cases": [
      {"name": "test_a", "code": "def test_a():\\n    assert 1\\n"},
      {"name": "test_b", "template": "T1", "slots": {"mode": "valid",
        "imports": "from calc import div", "target": "div({v}, 2)",
        "cases": [{"value": 1, "expected": 0}, {"value": 2, "expected": 1}]}}
    ]}"""
    data = gen_tests._parse_cases(raw)
    assert data is not None and len(data["cases"]) == 2
    # 无 def test_ 且无 template 的案仍被拒
    assert gen_tests._parse_cases('{"cases": [{"name": "x"}]}') is None


def test_build_user_prompt_template_section_gate():
    from test_agent.skills.prompt_library import build_user_prompt
    src = [("m.py", "def f():\n    pass\n")]
    on = build_user_prompt(src, "需求", templates=True)
    off = build_user_prompt(src, "需求", templates=False)
    assert "槽位模板通道" in on
    assert "槽位模板通道" not in off        # 对照组不泄漏模板示例（消融效度）


# ---------- T4 状态迁移（B4）与蜕变/状态迁移提示词开关（B8） ----------

T4_SLOTS = {
    "imports": "from order import Order",
    "setup": "order = Order('pending')",
    "steps": [{"call": "order.pay()", "assert": "order.status == 'paid'"},
              {"call": "order.ship()", "assert": "order.status == 'shipped'"}],
    "invalid": [{"call": "order.pay()", "error": "RuntimeError"}],
}


def test_t4_transition_render():
    code = _render("T4", T4_SLOTS, name="test_order")
    assert "def test_test_order_状态迁移():" in code
    assert "order = Order('pending')" in code
    assert "order.pay()" in code and "assert order.status == 'paid'" in code
    assert "with pytest.raises(RuntimeError):" in code   # 非法迁移段
    # 函数体顺序：setup → 迁移1 → 断言1 → 迁移2 → 断言2 → raises
    body = code[code.index("():"):]
    assert body.index("order.pay()") < body.index("assert order.status == 'paid'") \
        < body.index("order.ship()") < body.index("pytest.raises")


def test_t4_validation():
    with pytest.raises(SlotError, match="至少 2 条合法迁移"):
        render_slot_template("T4", {**T4_SLOTS, "steps": T4_SLOTS["steps"][:1]})
    with pytest.raises(SlotError, match="setup"):
        render_slot_template("T4", {**T4_SLOTS, "setup": "  "})
    with pytest.raises(SlotError, match="assert"):
        render_slot_template("T4", {**T4_SLOTS,
                                    "steps": T4_SLOTS["steps"] + [{"call": "order.deliver()"}]})


def test_expand_t4_and_method_tag():
    good = {"name": "test_order_walk", "template": "T4", "slots": T4_SLOTS}
    out, rejected = expand_template_cases([good])
    assert not rejected
    assert out[0]["method"] == KIND_METHOD["T4"]
    assert "def test_test_order_walk_状态迁移():" in out[0]["code"]


def test_prompt_sections_gated():
    from test_agent.skills.prompt_library import build_user_prompt
    src = [("m.py", "def f():\n    pass\n")]
    st_on = build_user_prompt(src, "需求", state_transition=True)
    mt_on = build_user_prompt(src, "需求", metamorphic=True)
    both_off = build_user_prompt(src, "需求")
    assert "状态迁移法" in st_on and "模板 T4" in st_on
    assert "蜕变关系" in mt_on and "CSUR" in mt_on
    assert "状态迁移法" not in both_off and "蜕变关系" not in both_off
