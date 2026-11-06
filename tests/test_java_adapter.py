"""白包阶段2·java_adapter（surefire XML→统一文本）与 JaCoCo 覆盖表解析（先红后绿）。

S5 是扩展枢纽：adapter 输出统一文本，下游 evaluate/repair/report 零改动（任务卡纪律）。
"""
import pytest

SUREFIRE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestGenR1" tests="3" failures="1" errors="1" skipped="0" time="0.05">
  <testcase name="test_add_pos" classname="TestGenR1" time="0.01"/>
  <testcase name="test_clamp_hi" classname="TestGenR1" time="0.01">
    <failure message="expected: &lt;10&gt; but was: &lt;11&gt;" type="org.opentest4j.AssertionFailedError">at TestGenR1.test_clamp_hi(TestGenR1.java:12)</failure>
  </testcase>
  <testcase name="test_load_x" classname="TestGenR1" time="0.01">
    <error message="ClassNotFound" type="java.lang.ClassNotFoundException">at TestGenR1(TestGenR1.java:20)</error>
  </testcase>
</testsuite>
"""

JACOCO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">
<report name="demo">
  <package name="com/demo">
    <class name="com/demo/OrderService" sourcefilename="OrderService.java">
      <counter type="INSTRUCTION" missed="10" covered="90"/>
      <counter type="LINE" missed="4" covered="36"/>
      <counter type="METHOD" missed="1" covered="9"/>
    </class>
    <class name="com/demo/Util" sourcefilename="Util.java">
      <counter type="LINE" missed="0" covered="10"/>
    </class>
  </package>
  <counter type="LINE" missed="4" covered="46"/>
</report>
"""


class TestSurefireAdapter:
    def test_counts_and_failed_lines(self):
        from test_agent.skills.java_adapter import surefire_dir_to_pytest_text
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "TEST-TestGenR1.xml").write_text(SUREFIRE_XML, encoding="utf-8")
            text = surefire_dir_to_pytest_text(d)
        assert "1 passed" in text and "1 failed" in text and "1 error" in text
        assert "FAILED TestGenR1::test_clamp_hi" in text
        assert "ERROR TestGenR1::test_load_x" in text

    def test_empty_dir_renders_zero(self):
        from test_agent.skills.java_adapter import surefire_dir_to_pytest_text
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            text = surefire_dir_to_pytest_text(d)
        assert "0 passed" in text

    def test_coverage_section_from_jacoco(self):
        from test_agent.skills.java_adapter import surefire_dir_to_pytest_text, jacoco_xml_to_rows
        rows, total = jacoco_xml_to_rows(JACOCO_XML)
        assert total == 92          # round(46/50)
        assert rows[0]["file"] == "OrderService.java"
        assert rows[0]["miss"] == 4 and rows[0]["cover"] == 36
        assert all(r["miss"] > 0 for r in rows)     # 只留有缺口文件行（B2 口径对齐）


def test_java_stack_lines_parsed_for_attribution():
    """S6：surefire failure 的 'at ...' 行进入统一文本（归因三闸消费堆栈首帧）。"""
    from test_agent.skills.java_adapter import surefire_dir_to_pytest_text
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "TEST-TestGenR1.xml").write_text(SUREFIRE_XML, encoding="utf-8")
        text = surefire_dir_to_pytest_text(d)
    assert "at TestGenR1.test_clamp_hi(TestGenR1.java:12)" in text
