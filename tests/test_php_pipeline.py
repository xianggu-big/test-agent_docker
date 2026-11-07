"""白包阶段4·PHP 接入测试矩阵（docs/多语言工具链接入任务包-20261106.md 阶段4）。

单测锁六位置的闸门与解析器；junit/clover 形态按 PHPUnit 11 文档契约构造。
真机全链路=scripts/smoke_php.py。
"""
import json

from test_agent.graph.nodes import _tier_strategy, build_run_cmd_php
from test_agent.graph.repair import classify_failure
from test_agent.skills.attribution import canary_cmd
from test_agent.skills.gen_tests import (
    PHP_CASE_RE,
    collect_gate_cmd,
    gen_file_naming,
)
from test_agent.skills.php_adapter import clover_to_rows, phpunit_junit_to_pytest_text
from test_agent.skills.builtin import probe_deps_manifest


def test_php_case_re():
    assert PHP_CASE_RE.search("public function test_add(): void {")
    assert PHP_CASE_RE.search("function testAdd() {")
    assert not PHP_CASE_RE.search("def test_x(): pass")
    assert not PHP_CASE_RE.search("func TestAdd(t *testing.T)")
    assert not PHP_CASE_RE.search('test("test_x", () => {})')


def test_gen_file_naming_php():
    assert gen_file_naming("php", 1) == ("TestGenR1.php", "phpunit")
    assert gen_file_naming("php", 2, feedback=True, fb_no=3) == ("TestGenFb3.php", "phpunit")


def test_collect_gate_cmd_php():
    assert collect_gate_cmd("php", "TestGenR1.php") == ["php", "-l", "TestGenR1.php"]


def test_probe_deps_manifest_composer(tmp_path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    assert probe_deps_manifest(tmp_path) == "composer_json"
    assert probe_deps_manifest(tmp_path / "empty") is None


# —— S5 php_adapter（junit 复用解析 + clover 覆盖表） ——

PHPJUNIT = """<testsuites>
  <testsuite name="TestGenR1" tests="2" failures="1" errors="0">
    <testcase name="test_add" classname="TestGenR1" time="0.001"/>
    <testcase name="test_tier_90" classname="TestGenR1" time="0.002">
      <failure message="Failed asserting that two strings are identical.">
Failed asserting that two strings are identical.
--- Expected
+++ Actual
@@ @@
-'A'
+'B'
      </failure>
    </testcase>
  </testsuite>
</testsuites>"""


def test_php_adapter_text_counts_and_failed():
    text = phpunit_junit_to_pytest_text(PHPJUNIT)
    assert "1 passed, 1 failed" in text
    assert "FAILED TestGenR1::test_tier_90" in text
    assert "assertSame" in text or "identical" in text


def test_php_adapter_clover_gaps_and_total():
    clover = """<coverage>
  <project>
    <file name="/work/src/Shop.php">
      <metrics statements="6" coveredstatements="4"/>
      <line num="9" type="stmt" count="0"/>
      <line num="12" type="stmt" count="1"/>
    </file>
    <file name="/work/src/Full.php">
      <metrics statements="2" coveredstatements="2"/>
    </file>
  </project>
</coverage>"""
    rows, total = clover_to_rows(clover)
    assert [r["file"] for r in rows] == ["/work/src/Shop.php"]   # B2 口径：全覆盖不进
    assert rows[0]["miss"] == 2 and rows[0]["missing"] == [9]
    assert total == 75   # 6/8


def test_php_adapter_clover_unparseable_honest():
    rows, total = clover_to_rows("<not-xml")
    assert rows == [] and total == 0


def test_php_adapter_total_always_emitted():
    # go 实锤教训：全覆盖时 TOTAL 也要如实输出（否则 evaluate 误读未采集）
    clover = """<coverage><project>
      <file name="/work/src/A.php"><metrics statements="2" coveredstatements="2"/></file>
    </project></coverage>"""
    text = phpunit_junit_to_pytest_text(PHPJUNIT, clover)
    import re
    assert re.search(r"TOTAL.*?\s(\d+)%", text)


# —— 归因：PHP fatal error/exception 分支（classify_failure 既有口径覆盖） ——

def test_classify_php_exception_is_case_or_product_by_form():
    # PHP fatal：undefined 引用/语法错 → case_defect（S6 分支）；断言失败 → product_defect
    assert classify_failure(255, "PHP Fatal error: Uncaught Error: Call to undefined function buy()", "") \
        == "case_defect"
    assert classify_failure(255, "PHP Parse error: syntax error, unexpected end of file", "") \
        == "case_defect"
    assert classify_failure(1, "FAILED TestGenR1::test_tier_90 - Failed asserting 'B' identical 'A'", "") \
        == "product_defect"


# —— 命令/分级/金丝雀 ——

def test_build_run_cmd_php():
    cmd = build_run_cmd_php(["TestGenR1.php"])
    assert cmd[0] == "vendor/bin/phpunit" and "TestGenR1.php" in cmd
    assert "--log-junit" in cmd and "--coverage-clover" in cmd


def test_tier_strategy_php_full():
    assert _tier_strategy("php") == "full"


def test_canary_cmd_php_shape():
    cmd = canary_cmd("php")
    assert cmd[0] == "php" and cmd[1] == "-r"
    assert "_canary.tmp" in cmd[2]
