# -*- coding: utf-8 -*-
"""D8：expected_basis=需求明示 的确定性校验（零重叠假标注降级模型推断）。"""
from test_agent.skills.gen_tests import _verify_expected_basis

REQ = "对被测项目做功能测试：输入校验（空值/越界）、正常路径、边界值；行覆盖率目标 70%。"


def test_零重叠假标注被降级():
    cases = [{"name": "test_a", "expected": "中间 AND 转为 and", "input": "全大写小词",
              "expected_basis": "需求明示", "code": "def test_a():\n    pass\n"}]
    downgraded = _verify_expected_basis(cases, REQ)
    assert downgraded == ["test_a"]
    assert cases[0]["expected_basis"] == "模型推断"


def test_有重叠的真标注保留():
    cases = [{"name": "test_b", "expected": "输入为空值时拒绝", "input": "空值输入",
              "expected_basis": "需求明示", "code": "def test_b():\n    pass\n"}]
    downgraded = _verify_expected_basis(cases, REQ)
    assert downgraded == []
    assert cases[0]["expected_basis"] == "需求明示"


def test_模板用例不校验():
    """模板形态的 expected_basis 由渲染器语义负责，零重叠也不动。"""
    cases = [{"name": "test_t", "expected": "见各点预期", "expected_basis": "需求明示",
              "template": "T2", "slots": {}}]
    assert _verify_expected_basis(cases, REQ) == []
    assert cases[0]["expected_basis"] == "需求明示"


def test_无需求或其他标注不动():
    cases = [{"name": "test_c", "expected": "中间 AND 转为 and", "expected_basis": "需求明示",
              "code": "def test_c():\n    pass\n"},
             {"name": "test_d", "expected": "任意", "expected_basis": "源码推导",
              "code": "def test_d():\n    pass\n"}]
    assert _verify_expected_basis(cases, "") == []          # 无需求无法校验，诚实不判
    assert cases[0]["expected_basis"] == "需求明示"
    assert cases[1]["expected_basis"] == "源码推导"          # 其他标注不受影响
