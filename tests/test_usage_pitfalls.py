"""三源注入 A/B 源（docs/领域知识注入调研-20261031.md）：
A 源=repo 调用形态锚点（剥断言=防迁就切分线，kill 检查锁定）；
B 源=语言×类型陷阱库（curated 清单+需求路由选条目+expected_basis 判据行）。"""
from pathlib import Path

from test_agent.skills.gen_tests import _extract_usage_examples
from test_agent.skills.prompt_library import (PITFALL_KB, build_pitfall_section,
                                              build_user_prompt, select_pitfalls)


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "calc.py").write_text(
        "def add(a, b):\n"
        '    """两数相加。"""\n'
        "    return a + b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "import pytest\n"
        "from calc import add, div\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "\n"
        "def test_div_zero():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        div(1, 0)\n", encoding="utf-8")
    return ws


def test_usage_examples_strip_asserts_kill_check(tmp_path):
    """kill 检查（防自欺，调研文档§每源一 kill）：残片必须不含具体预期值/断言——
    剥除断言是防迁就切分线（对齐自带测试预期=对齐实现现状，2412.14137）。"""
    ws = _make_ws(tmp_path)
    out = _extract_usage_examples(ws)
    assert out is not None
    assert "from calc import" in out          # 被测包的 import 锚定（调用形态）
    assert "div(1, 0)" in out                 # 调用表达式保留
    assert "assert add(1, 2) == 3" not in out  # 断言（预期值）剥离
    assert "== 3" not in out
    for line in out.splitlines():             # 逐行核验：没有任何 assert 行
        assert not line.strip().startswith("assert")


def test_usage_examples_empty_repo_returns_none(tmp_path):
    """无自带测试 → None（诚实：C 源空 docstring 同款语义）。"""
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert _extract_usage_examples(ws) is None


def test_usage_examples_prompt_injection(tmp_path):
    ws = _make_ws(tmp_path)
    out = _extract_usage_examples(ws)
    p = build_user_prompt([("calc.py", "x=1")], "需求", usage_examples=out)
    assert "调用形态锚点" in p and "不是预期值来源" in p and "from calc import" in p
    # 基线（开关语义在 generate_tests 走 config 门）提示词不含该段
    assert "调用形态锚点" not in build_user_prompt([("calc.py", "x=1")], "需求")


def test_pitfall_kb_structure():
    """清单结构契约：每条 {pattern, why, test_hint, keyword} 齐全（curated 清单的质量闸）。"""
    for track, items in PITFALL_KB.items():
        assert track in ("python", "web", "cli", "go")   # F2 阶段 1 #3：Go 池入册
        assert len(items) >= 4
        for item in items:
            assert item["pattern"] and item["why"] and item["test_hint"] and item["keyword"]


def test_select_pitfalls_routing():
    assert select_pitfalls("python", "下单接口 FastAPI 响应")[0] in PITFALL_KB["web"]      # 需求像 Web
    assert select_pitfalls("python", "命令行工具 argparse")[0] in PITFALL_KB["cli"]        # 需求像 CLI
    assert select_pitfalls("python", "计算器加法")[0] in PITFALL_KB["python"]              # 默认按语言
    assert len(select_pitfalls("python", "计算器")) <= 4                                   # 条数封顶


def test_pitfall_section_expected_basis_discipline():
    """B 源判据（复审修正②）：陷阱只教"怎么测"不教"预期是什么"——注入段必须带
    expected_basis 实质来源纪律，否则消融时依据会大量标「常识」。"""
    sec = build_pitfall_section("python", "浮点折扣计算")
    assert "领域陷阱清单" in sec and "expected_basis" in sec and "常识" in sec
    assert build_user_prompt([("a.py", "x=1")], "需求", pitfalls=True,
                             module_lang="python").count("领域陷阱清单") == 1


# ---------- 任务卡-F2 阶段 1 #3：Go 陷阱条目（error shadowing/nil map/goroutine 泄漏） ----------

def test_go陷阱条目_语言路由命中():
    from test_agent.skills.prompt_library import select_pitfalls
    items = select_pitfalls("go", "订单分级服务：按积分分级")
    assert items and items is not select_pitfalls("python", "订单分级服务")
    patterns = {p["pattern"] for p in select_pitfalls("go", "通用函数测试")}
    assert "goroutine 泄漏" in patterns and any("shadowing" in p for p in patterns)


def test_go陷阱段_渲染含测试手法(tmp_path):
    from test_agent.skills.prompt_library import build_pitfall_section
    section = build_pitfall_section("go", "缓存查询服务")
    assert "领域陷阱清单" in section
    # 需求命中 web 关键词时按既有规则仍走 web 池（语言回退不覆盖需求路由）——锁语义
    assert ("nil map" in section) or ("缓存穿透" in section)
