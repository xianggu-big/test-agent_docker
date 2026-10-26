"""repair.py 纯函数单测 —— R-Loop 三停（准出/熔断/预算）与归因三分类的逻辑保证。

运行：pytest tests/ -q（宿主侧，需 requirements-dev.txt）
这些测试是论文「归因正确率/补测闭环」实验的合规底线：判定逻辑改变必须先过这里。
"""
import pytest

from test_agent.graph import repair
from test_agent.graph.repair import classify_failure, decide, fingerprint_of


# ---------- fingerprint_of ----------

class TestFingerprint:
    def test_断言值变化但同一失败点_同指纹(self):
        a = "FAILED test_calc.py::test_add - assert 3 == 5"
        b = "FAILED test_calc.py::test_add - assert 3 == 4"
        assert fingerprint_of(a, "") == fingerprint_of(b, "")

    def test_不同用例失败_异指纹(self):
        a = "FAILED test_calc.py::test_add - assert 3 == 5"
        b = "FAILED test_calc.py::test_div - assert 2 == 5"
        assert fingerprint_of(a, "") != fingerprint_of(b, "")

    def test_错误类型只留类名(self):
        a = "FAILED t.py::test_x - ModuleNotFoundError: No module named 'requests'"
        b = "FAILED t.py::test_x - ModuleNotFoundError: No module named 'flask'"
        assert fingerprint_of(a, "") == fingerprint_of(b, "")

    def test_无摘要时退化为输出尾部哈希_且稳定(self):
        out = "some crash traceback\nline2\nline3"
        fp1 = fingerprint_of("", out)
        fp2 = fingerprint_of("", out)
        assert fp1 == fp2 and fp1

    def test_空输入_空指纹(self):
        assert fingerprint_of("", "") == ""

    def test_多行失败摘要_与顺序无关(self):
        a = "FAILED t.py::test_a - assert 1 == 2\nFAILED t.py::test_b - assert 3 == 4"
        b = "FAILED t.py::test_b - assert 3 == 4\nFAILED t.py::test_a - assert 1 == 2"
        assert fingerprint_of(a, "") == fingerprint_of(b, "")


# ---------- classify_failure ----------

class TestClassify:
    def test_通过为none(self):
        assert classify_failure(0, "1 passed", "") == "none"

    def test_缺pytest为env(self):
        assert classify_failure(1, "", "/bin/sh: pytest: not found") == "env"

    def test_no_module_named_pytest为env(self):
        assert classify_failure(1, "No module named pytest", "") == "env"

    def test_没有可跑用例为case_defect(self):
        assert classify_failure(5, "no tests ran in 0.01s", "") == "case_defect"

    def test_用例引用不存在的东西为case_defect(self):
        assert classify_failure(1, "ImportError: cannot import name 'foo'", "") == "case_defect"

    def test_断言失败为疑似产品缺陷(self):
        assert classify_failure(1, "FAILED t.py::test_x - AssertionError", "") == "product_defect"

    def test_未知失败默认按env处理(self):
        assert classify_failure(1, "some weird output", "") == "env"


# ---------- decide（三停 + 分流）----------

PASS_OUT = "3 passed"
FAIL_OUT = "1 failed, 2 passed\nFAILED t.py::test_x - assert 3 == 5"
FP = "abc123"


class TestDecide:
    def test_准出标准_通过且覆盖达标(self):
        v, _, r = decide(0, PASS_OUT, "", [], "", 1, 0.90, 0.70)
        assert v == "pass" and "达标" in r

    def test_A8_覆盖率解析失败不放行_走补测(self):
        v, _, r = decide(0, PASS_OUT, "", [], "", 1, None, 0.70)
        assert v == "repair_gen" and "未知" in r

    def test_通过但覆盖不足_补测(self):
        v, _, r = decide(0, PASS_OUT, "", [], "", 1, 0.50, 0.70)
        assert v == "repair_gen" and "补测" in r

    def test_A1_任务行阈值生效_低阈值即达标(self):
        v, _, _ = decide(0, PASS_OUT, "", [], "", 1, 0.60, 0.50)
        assert v == "pass"

    def test_同指纹第二次出现即熔断(self):
        v, _, r = decide(1, FAIL_OUT, "", [FP], FP, 1, None, 0.70)
        assert v == "stop" and "熔断" in r

    def test_空指纹不参与熔断(self):
        # 指纹为空（无失败摘要）时不得误触发熔断，应继续走预算/分流
        v, _, _ = decide(1, "", "", [""], "", 1, None, 0.70)
        assert v != "stop"

    def test_预算耗尽_未通过如实上报(self):
        v, _, r = decide(1, FAIL_OUT, "", [], FP, 3, None, 0.70)
        assert v == "stop" and "预算耗尽" in r

    def test_预算耗尽_通过但覆盖未达标如实上报(self):
        v, _, r = decide(0, PASS_OUT, "", [], "", 3, 0.50, 0.70)
        assert v == "stop" and "预算耗尽" in r

    def test_env失败分流修复环境(self):
        v, kind, _ = decide(1, "", "/bin/sh: pip: not found", [], FP, 1, None, 0.70)
        assert v == "repair_env" and kind == "env"

    def test_产品类失败分流重生成用例(self):
        v, kind, _ = decide(1, FAIL_OUT, "", [], FP, 1, None, 0.70)
        assert v == "repair_gen" and kind == "product_defect"


# ---------- A5 def 名规整（借用 langchain 已装则测，未装则跳过） ----------

class TestSanitizeName:
    def test_名字规整_唯一_可回查(self):
        pytest.importorskip("langchain_openai")
        from test_agent.skills.gen_tests import _sanitize_name
        used: set[str] = set()
        assert _sanitize_name("登录正常流程 测试", 0, used) == "test_登录正常流程_测试"
        first = _sanitize_name("边界值分析", 1, used)
        second = _sanitize_name("边界值分析", 2, used)
        assert first.startswith("test_") and first != second  # 同名自动去重
