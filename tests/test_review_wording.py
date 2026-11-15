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


def test_红线_failed大于零却称全部通过_专锁():
    """MutA 专锁（体检 P2-8：该红线原单点耦合）——删除此红线本测试必红。
    样本设计：verdict=pass（绕开 stop 黑名单线）+叙述只含『全部通过』——
    唯一能触发的是 failed>0 红线本身。"""
    m = {"passed": 2, "failed": 3, "skipped": 0, "coverage_line": 0.5,
         "pass_rate": 0.4, "failure_kind": "none"}
    reasons = review_rules("全部通过。", m, "pass")
    assert any("全部通过" in r for r in reasons), reasons


def test_红线_failed大于零全部通过_变体二():
    """MutA 第二条守线（P2-8 加厚）：不同 failed 值/叙述位置的变体。"""
    m = {"passed": 5, "failed": 1, "skipped": 0, "coverage_line": 0.9,
         "pass_rate": 0.833, "failure_kind": "none"}
    reasons = review_rules("覆盖率 90%。全部通过，收工。", m, "pass")
    assert any("全部通过" in r for r in reasons), reasons


def test_通过率分数形式零分母不炸():
    """叙述出现"通过率 3/0"（LLM 手滑/对抗样本）不得 ZeroDivisionError 炸穿规则闸
    （该闸在 review_report 的 LLM try 之前）。零分母的分数形式直接忽略。"""
    from test_agent.skills.evaluate import review_rules
    reasons = review_rules("通过率 3/0，全部通过", {"pass_rate": 1.0}, "pass")
    assert isinstance(reasons, list)
