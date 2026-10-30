# -*- coding: utf-8 -*-
"""P1-3 修复回归：review_rules 措辞变体表驱动（通过率 N% 白名单比对 + 无遗留类黑名单）。
EXP1-A 场景（"通过率 100%" 逃逸）修复后必须产出非空 reasons。"""
import pytest

from test_agent.skills.evaluate import review_rules

M = {"passed": 2, "failed": 3, "skipped": 0, "coverage_line": 0.5,
     "pass_rate": 0.4, "failure_kind": "product_defect"}


@pytest.mark.parametrize("narrative", [
    "本轮通过率 100%，所有功能均已验证，无遗留问题。",   # EXP1-A 原始逃逸样本
    "通过率 90%，结果良好。",
    "服务通过率 33/34（约 97%）。",
])
def test_通过率与数据不符被拦(narrative):
    reasons = review_rules(narrative, M, "stop")
    assert any("通过率" in r for r in reasons), narrative


def test_通过率与数据相符放行():
    assert review_rules("本轮通过率 40%，覆盖率 50%。", M, "stop") == []


def test_无pass_rate时编造通过率被拦():
    m = {"passed": 2, "failed": 3, "coverage_line": 0.5}  # 无 pass_rate
    reasons = review_rules("通过率 88%，一切正常。", m, "stop")
    assert any("无 pass_rate" in r for r in reasons)


@pytest.mark.parametrize("narrative", [
    "所有功能均已验证，无遗留问题。",
    "本轮测试验证完毕，质量达标。",
    "缺陷均已修复，无遗留。",
])
def test_无遗留类同义措辞在stop下被拦(narrative):
    reasons = review_rules(narrative, M, "stop")
    assert any("防造假红线" in r for r in reasons), narrative


def test_无遗留类在pass下放行():
    """pass 终态说'无遗留问题'是正常结论，不拦。"""
    m = dict(M, failure_kind="none")
    assert review_rules("全部用例通过，无遗留问题。", m, "pass") == []


def test_P3_1_拦截消息不再含正则碎片():
    reasons = review_rules("通过 9、失败 0。", M, "stop")
    assert reasons and all("?<" not in r for r in reasons)
