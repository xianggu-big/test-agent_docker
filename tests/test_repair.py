"""R-Loop 核心纯函数回归网：失败指纹 / 归因分类 / 停止判定。

这组测试防的是系统最脆弱的一环：解析依赖 pytest 文本输出格式。
fixture 均为真实运行的输出片段（pytest 9.1.1，镜像内版本）——
若镜像内工具链升级导致格式变化，这里会最先变红，而不是线上熔断静默失效。

判定义务（2026-10-27 语义升级后）：
- 疑似产品缺陷 → 立即冻结上报（断言失败=缺陷证人，不回炉迁就，防造假）
- 覆盖率未知 ≠ 达标 → 补测一轮获取数据（诚实边界）
- 指纹熔断 / 轮次预算对 用例缺陷与环境失败 生效
"""
from test_agent import config
from test_agent.graph import repair

# —— 真实输出 fixture（源自任务 3/6 实际运行记录） ——

PASS_STDOUT = """\
..                                                                        [100%]
2 passed in 0.01s

Name      Stmts   Miss  Cover   Missing
---------------------------------------
calc.py       4      0   100%
---------------------------------------
TOTAL         4      0   100%
"""

FAIL_STDOUT = """\
.F                                                                        [100%]
1 failed, 1 passed in 0.02s

FAILED test_calc.py::test_div_wrong_expectation - AssertionError: assert 2.0 == 5
"""

COLLECT_ERROR_STDERR = """\
ERRORS
_____________ ERROR collecting test_calc.py _____________
ImportError while importing test module 'test_calc.py'.
E   ModuleNotFoundError: No module named 'calc'
"""


# —— 指纹 ——

def test_fingerprint_stable_across_assert_values():
    """设计目标：同一用例同一错误类型，断言数值不同 → 同指纹（该熔断就熔断）。"""
    a = repair.fingerprint_of(
        "FAILED test_calc.py::test_div - AssertionError: assert 2.0 == 5\n", "")
    b = repair.fingerprint_of(
        "FAILED test_calc.py::test_div - AssertionError: assert 9.9 == 5\n", "")
    assert a and a == b


def test_fingerprint_differs_by_nodeid_or_errtype():
    a = repair.fingerprint_of("FAILED t.py::test_x - AssertionError: a\n", "")
    b = repair.fingerprint_of("FAILED t.py::test_y - AssertionError: a\n", "")
    c = repair.fingerprint_of("FAILED t.py::test_x - TypeError: a\n", "")
    assert a != b and a != c


def test_fingerprint_multi_failures_order_insensitive():
    a = repair.fingerprint_of(
        "FAILED t.py::test_x - AssertionError: a\nFAILED t.py::test_y - TypeError: b\n", "")
    b = repair.fingerprint_of(
        "FAILED t.py::test_y - TypeError: b\nFAILED t.py::test_x - AssertionError: a\n", "")
    assert a == b


def test_fingerprint_empty_when_no_failure_text():
    assert repair.fingerprint_of("", "") == ""


def test_fingerprint_falls_back_to_tail_hash():
    a = repair.fingerprint_of("", "some crash tail\nline2")
    b = repair.fingerprint_of("", "some crash tail\nline2")
    assert a and a == b


def test_failing_nodeids_extracts_locations():
    ids = repair.failing_nodeids(FAIL_STDOUT)
    assert ids == ["test_calc.py::test_div_wrong_expectation"]


# —— 归因 ——

def test_classify_none_when_exit_zero():
    assert repair.classify_failure(0, PASS_STDOUT, "") == "none"


def test_classify_env_missing_pytest():
    assert repair.classify_failure(4, "", "No module named 'pytest'") == "env"


def test_classify_case_defect_no_tests():
    assert repair.classify_failure(5, "no tests ran in 0.01s", "") == "case_defect"


def test_classify_case_defect_import_error():
    assert repair.classify_failure(2, "", COLLECT_ERROR_STDERR) == "case_defect"


def test_classify_product_defect_assertion():
    assert repair.classify_failure(1, FAIL_STDOUT, "") == "product_defect"


def test_classify_unknown_defaults_to_env():
    assert repair.classify_failure(1, "", "segmentation fault") == "env"


# —— 判定（R-Loop 停止条件，按当前语义） ——

def _decide(**kw):
    base = dict(exit_code=0, stdout=PASS_STDOUT, stderr="",
                fingerprints=[], fingerprint="", round_no=1, coverage_line=1.0)
    base.update(kw)
    return repair.decide(**base)


def test_decide_pass_when_green_and_coverage_met():
    verdict, kind, _ = _decide()
    assert verdict == "pass" and kind == "none"


def test_decide_coverage_unknown_is_not_pass():
    """诚实边界：覆盖率解析失败不算达标，补测一轮拿数据。"""
    verdict, _, reason = _decide(coverage_line=None)
    assert verdict == "repair_gen"
    assert "未知" in reason


def test_decide_repair_gen_when_coverage_below_target():
    verdict, _, reason = _decide(coverage_line=0.5)
    assert verdict == "repair_gen"
    assert "补测" in reason


def test_decide_honors_frozen_task_target():
    """任务级冻结阈值（A1）：任务冻结 90% 时 80% 不达标。"""
    verdict, _, _ = _decide(coverage_line=0.8, coverage_target=0.9)
    assert verdict == "repair_gen"


def test_decide_product_defect_stops_immediately_as_witness():
    """疑似产品缺陷：第 1 次失败即停止上报，不重造用例迁就（防造假）。"""
    verdict, kind, reason = _decide(exit_code=1, stdout=FAIL_STDOUT, fingerprint="p1")
    assert verdict == "stop" and kind == "product_defect"
    assert "证人" in reason or "疑似产品缺陷" in reason


def test_decide_breaker_on_repeated_case_defect_fingerprint(monkeypatch):
    from test_agent import config as _cfg
    monkeypatch.setattr(_cfg, "FINGERPRINT_BREAKER", 2)   # 同上：钉阈值
    """指纹熔断对用例自身缺陷生效：同指纹出现 2 次即停。"""
    fp = "abc123"
    verdict, kind, reason = _decide(exit_code=2, stderr=COLLECT_ERROR_STDERR,
                                    fingerprints=[fp], fingerprint=fp)
    assert verdict == "stop" and kind == "case_defect"
    assert "熔断" in reason


def test_decide_breaker_on_repeated_env_fingerprint(monkeypatch):
    from test_agent import config as _cfg
    monkeypatch.setattr(_cfg, "FINGERPRINT_BREAKER", 2)   # 阈值语义单测：显式钉阈值不随默认漂移
    fp = "e999"
    verdict, _, reason = _decide(exit_code=4, stderr="pipeline crash",
                                 fingerprints=[fp], fingerprint=fp)
    assert verdict == "stop" and "熔断" in reason


def test_decide_first_case_defect_failure_repairs():
    verdict, kind, _ = _decide(exit_code=2, stderr=COLLECT_ERROR_STDERR, fingerprint="c1")
    assert verdict == "repair_gen" and kind == "case_defect"


def test_decide_first_env_failure_routes_to_repair_env():
    verdict, kind, _ = _decide(exit_code=4, stderr="No module named 'pytest'", fingerprint="e1")
    assert verdict == "repair_env" and kind == "env"


def test_decide_budget_exhausted_for_case_defect():
    verdict, _, reason = _decide(exit_code=2, stderr=COLLECT_ERROR_STDERR,
                                 fingerprint="c2", round_no=config.MAX_REPAIR_ROUNDS)
    assert verdict == "stop"
    assert "预算" in reason


def test_decide_budget_with_low_coverage():
    verdict, _, reason = _decide(coverage_line=0.5, round_no=config.MAX_REPAIR_ROUNDS)
    assert verdict == "stop"
    assert "预算" in reason


# —— V8：证人冻结 + 继续补测（witness_nodeids 传入即开关开） ——

def test_fingerprint_excludes_frozen_witness():
    """V8：冻结证人的失败行不进指纹——只剩证人失败时指纹为空（不熔断）。"""
    only_witness = repair.fingerprint_of(
        "FAILED t.py::test_w - AssertionError: assert 2.0 == 5\n", "",
        exclude_nodeids=("t.py::test_w",))
    assert only_witness == ""
    with_other = repair.fingerprint_of(
        "FAILED t.py::test_w - AssertionError: assert 2.0 == 5\n"
        "FAILED t.py::test_x - TypeError: boom\n", "",
        exclude_nodeids=("t.py::test_w",))
    assert with_other == repair.fingerprint_of("FAILED t.py::test_x - TypeError: boom\n", "")


def test_assertion_failure_nodeids_picks_assertion_only():
    """证人候选只认断言失败；ImportError 等用例自身坏掉的强信号不算证人。"""
    out = repair.assertion_failure_nodeids(
        "FAILED t.py::test_a - AssertionError: assert 1 == 2\n"
        "FAILED t.py::test_b - ImportError: No module named 'x'\n"
        "some other line\n")
    assert out == ["t.py::test_a"]


def test_decide_v8_continues_when_only_witness_fails_and_coverage_low():
    verdict, kind, reason = _decide(
        exit_code=1, stdout=FAIL_STDOUT, fingerprint="",
        coverage_line=0.5, witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "repair_gen" and kind == "product_defect"
    assert "继续补测" in reason


def test_decide_v8_stops_with_witness_report_when_coverage_met():
    """V8 终态：非证人全过+覆盖达标 → stop，证人如实上报（不叫 pass）。"""
    verdict, kind, reason = _decide(
        exit_code=1, stdout=FAIL_STDOUT, fingerprint="",
        coverage_line=1.0, witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "stop" and kind == "product_defect"
    assert "证人" in reason and "如实上报" in reason


def test_decide_v8_coverage_unknown_continues():
    verdict, _, reason = _decide(
        exit_code=1, stdout=FAIL_STDOUT, fingerprint="",
        coverage_line=None, witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "repair_gen" and "继续补测" in reason


def test_decide_v8_budget_exhausted_stops_with_witness():
    """V8 的继续补测不绕过预算：耗尽即停，证人照常如实上报。"""
    verdict, _, reason = _decide(
        exit_code=1, stdout=FAIL_STDOUT, fingerprint="",
        round_no=config.MAX_REPAIR_ROUNDS, coverage_line=0.5,
        witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "stop" and "预算" in reason and "证人" in reason


def test_decide_v8_new_non_witness_failure_breaks_normally(monkeypatch):
    """证人不豁免新失败：非证人同指纹重复仍熔断（防真打转被 V8 掩护）。"""
    from test_agent import config as _cfg
    monkeypatch.setattr(_cfg, "FINGERPRINT_BREAKER", 2)   # 阈值语义显式钉住，不随默认漂移
    stdout = ("FAILED test_calc.py::test_div_wrong_expectation - AssertionError: assert 2.0 == 5\n"
              "FAILED t.py::test_new - TypeError: boom\n")
    fp = repair.fingerprint_of(stdout, "",
                               exclude_nodeids=("test_calc.py::test_div_wrong_expectation",))
    verdict, _, reason = _decide(
        exit_code=1, stdout=stdout, fingerprints=[fp], fingerprint=fp,
        coverage_line=1.0, witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "stop" and "熔断" in reason


def test_decide_v8_witness_birth_round_with_other_failure_routes_repair():
    """证人产生当轮（evaluate 先累积再判定）+非证人失败 → 常规 repair_gen 分流。"""
    stdout = ("FAILED test_calc.py::test_div_wrong_expectation - AssertionError: assert 2.0 == 5\n"
              "FAILED t.py::test_x - TypeError: boom\n")
    verdict, kind, _ = _decide(
        exit_code=1, stdout=stdout, fingerprint="f1", coverage_line=0.5,
        witness_nodeids=["test_calc.py::test_div_wrong_expectation"])
    assert verdict == "repair_gen" and kind == "product_defect"
