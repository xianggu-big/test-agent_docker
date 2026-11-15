# -*- coding: utf-8 -*-
"""E9 近期任务错误清扫的修复单测（2026-11-15，用户令"逐一修复"）：

① RobustChat 连接错误退避穷尽 → 本地备胎接管（打标 primary_unreachable）
② 归因：C 扩展未编译等环境构建信号 → env（不再误判 product_defect 证人）
③ gen 解析边界字段类型归一（expected=list / input=dict 不再炸 2-gram 拼接）
④ estimate_tokens 分段估算（符号密集英文代码不再低估 ~25%）
⑤ run_tests 容器缺失 → 结构化环境错误（裸 docker 404 不再直抛）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.repair import classify_failure
from test_agent.skills.prompt_library import estimate_tokens


# ---------- ① 连接错误备胎 ----------

class _FlakyInner:
    """永远抛连接错误；记录调用次数。"""

    def __init__(self):
        self.calls = 0

    def invoke(self, messages, *a, **kw):
        self.calls += 1
        raise ConnectionError("APIConnectionError: Connection error.")


class _FallbackOK:
    def invoke(self, messages, *a, **kw):
        class _R:
            content = "local-rescued"

            class additional_kwargs:  # noqa: N801 —— dict 占位由打标逻辑替换
                pass
        r = _R()
        r.additional_kwargs = {}
        return r


def test_连接错误穷尽_走备胎并打标(monkeypatch):
    from test_agent.skills import llm_client as lc
    from test_agent import config
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True, raising=False)

    inner = _FlakyInner()
    rc = lc.RobustChat(inner, retries=2, phase="gen_tests")
    monkeypatch.setattr(rc, "_try_fallback",
                        lambda messages, *a, reason="x", **kw: _captured.update(
                            {"reason": reason}) or _FallbackOK().invoke(messages))
    _captured: dict = {}
    resp = rc.invoke([("user", "hi")])
    assert inner.calls == 2                      # 重试穷尽
    assert resp.content == "local-rescued"       # 备胎接管
    assert _captured["reason"] == "primary_unreachable"


def test_连接错误_备胎关闭时照常抛RateLimited(monkeypatch):
    from test_agent.skills import llm_client as lc
    from test_agent import config
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", False, raising=False)
    rc = lc.RobustChat(_FlakyInner(), retries=1, phase="gen_tests")
    try:
        rc.invoke([("user", "hi")])
        raise AssertionError("应抛 RateLimited")
    except lc.RateLimited as e:
        assert "APIConnectionError" in str(e)


def test_402备胎_reason保持原有语义(monkeypatch):
    from test_agent.skills import llm_client as lc
    from test_agent import config
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True, raising=False)
    rc = lc.RobustChat(_FlakyInner(), retries=1, phase="gen_tests")
    captured: dict = {}
    monkeypatch.setattr(rc, "_try_fallback",
                        lambda messages, *a, reason="x", **kw: captured.update(
                            {"reason": reason}) or None)   # 备胎失败 → 原错误照抛
    try:
        rc.invoke([("user", "hi")])
        raise AssertionError("应抛 RateLimited")
    except lc.RateLimited:
        pass
    assert captured["reason"] == "primary_unreachable"     # 连接类 reason 不再是 402 语义


# ---------- ② 环境构建信号归因 ----------

def test_C扩展未编译_归env():
    stderr = ('ERROR test_wcwidth_x.py::test_a - Failed: '
              'failed on setup with "AssertionError: C extension not loaded in extension mode"')
    assert classify_failure(1, "", stderr) == "env"


def test_编译器缺失家族_归env():
    assert classify_failure(1, "", "error: Microsoft Visual C++ 14.0 is required") == "env"
    assert classify_failure(1, "", "error: Unable to find vcvarsall.bat") == "env"


def test_普通断言失败_仍归product_defect():
    # 既有口径不回退：行为断言失败依然是疑似产品缺陷
    assert classify_failure(1, "assert add(1, 2) == 3\nFAILED", "") == "product_defect"


def test_裸failed_on_setup_不入环境表():
    # 语义两可（可能是用例侧坏 fixture）——诚实边界：不凭裸 setup 字样归 env
    text = 'failed on setup with "AssertionError: expect 1 got 2"'
    assert classify_failure(1, text, "") == "product_defect"


def test_差分闸证据表含新信号():
    from test_agent.skills.attribution import _ENV_FAIL_HINTS, _scan_hints
    assert _scan_hints("AssertionError: C extension not loaded in extension mode",
                       _ENV_FAIL_HINTS) == ["C extension not loaded"]


# ---------- ③ 解析边界字段类型归一 ----------

def test_parse_cases_expected_list_归一为JSON文本():
    from test_agent.skills.gen_tests import _parse_cases
    raw = json.dumps({
        "imports": "import pytest",
        "cases": [{"name": "test_a", "method": "等价类",
                   "code": "def test_a():\n    assert True",
                   "input": {"coupon": "X"},
                   "steps": "调用",
                   "expected": ["返回通过", "状态码 200"],
                   "expected_basis": "需求明示"}]}, ensure_ascii=False)
    data = _parse_cases(raw, lang=None)
    assert data is not None
    c = data["cases"][0]
    assert isinstance(c["expected"], str) and "返回通过" in c["expected"]
    assert isinstance(c["input"], str) and '"coupon"' in c["input"]


def test_归一后_2gram拼接不再炸():
    from test_agent.skills.gen_tests import _verify_expected_basis
    cases = [{"name": "test_a", "expected": ["返回通过", "状态码 200"],
              "input": {"coupon": "X"}, "expected_basis": "需求明示"}]
    downgraded = _verify_expected_basis(cases, "优惠券校验需求")
    assert isinstance(downgraded, list)              # 不炸即过（降级与否取决于 2-gram）


# ---------- ④ 分段估算 ----------

def test_估算_符号密集代码不再低估():
    code = ("def f(a, b):\n"
            "    x = compute(a, b) + delta;\n"
            "    return clamp(x, lo=0, hi=100);\n") * 10
    est = estimate_tokens(code)
    assert est > len(code) / 2.2                     # 新估算高于旧全局系数（保守方向）


def test_估算_中文按1token每字():
    cn = "这是一段中文需求描述。" * 20
    assert estimate_tokens(cn) >= len(cn) * 0.9      # CJK≈1/char（旧 2.2 严重低估）


def test_估算_空串():
    assert estimate_tokens("") == 1


# ---------- ⑤ run_tests 容器缺失结构化 ----------

def test_run_tests_容器缺失_结构化env错误():
    from test_agent.skills import builtin

    class _BoomClient:
        class containers:  # noqa: N815
            @staticmethod
            def get(name):
                raise RuntimeError("404 Client Error: Not Found")

    class _Runner:
        client = _BoomClient()

    skills = builtin.build_skills(_Runner)
    out = json.loads(skills["run_tests"].func(1, {"container_name": "mta-run-x",
                                                  "cmd": ["pytest", "-q"]}))
    assert out["exit"] == -1 and "环境错误" in out["stderr"] and "mta-run-x" in out["stderr"]
