"""L1 服务编排兜底测试锁（2026-11-15 存疑 33/34 拍板全修）。

实锤链（六语言真机验收 1158/1160）：java/php 生成用例走真 HTTP 而 L1 没人起服务
→ java ConnectException（归 env 正确但 repair_env 只重建容器救不活）/php curl
code=0 断言失败（文本无"连接"字样 → 误归"疑似产品缺陷"首轮即停）。三修：
①parse_junit_xml 断言详情行进 message（php junit text="Failed asserting that 0
is identical to 200." 是唯一指纹）②classify_failure 补 curl code=0 特征 → env
③provision repair_env 回炉轮起服务（端口从用例解析，复用 L2 启动器）。
"""
from pathlib import Path

from test_agent.graph.nodes import (extract_service_port, php_exit_for_decide,
                                    start_service_for_repair)
from test_agent.graph.repair import classify_failure, decide
from test_agent.skills.case_results import parse_junit_xml


# ---------- ① parse_junit_xml：php junit 断言详情行进 message ----------

PHP_JUNIT = """<testsuites><testsuite name="TestGenR1" tests="1" failures="1">
<testcase name="test_get_ping" classname="TestGenR1" time="0.01">
<failure type="PHPUnit\\Framework\\ExpectationFailedException">TestGen1::test_get_ping
Failed asserting that 0 is identical to 200.

/work/TestGenR1.php:16</failure>
</testcase></testsuite></testsuites>"""


def test_parse_junit_php_assertion_line_wins_over_file_line():
    rows = parse_junit_xml(PHP_JUNIT)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    msg = rows[0]["message"]
    assert "Failed asserting that 0 is identical to 200" in msg, \
        "断言详情行必须进 message（curl code=0 指纹在断言行，归因特征靠它）"
    assert not msg.endswith(":16"), "不得只留 file:line 尾行"


def test_parse_junit_pytest_form_unchanged():
    """pytest junit（message 属性有值）走 short 分支——既有形态逐字节不变。"""
    pytest_junit = ('<testsuites><testsuite name="t" tests="1" failures="1">'
                    '<testcase name="test_a" classname="t" time="0.01">'
                    '<failure message="assert 1 == 2" type="AssertionError">'
                    'traceback lines...\n test_a.py:2: AssertionError</failure>'
                    '</testcase></testsuite></testsuites>')
    rows = parse_junit_xml(pytest_junit)
    assert rows[0]["message"].startswith("assert 1 == 2") or "assert 1 == 2" in rows[0]["message"]


# ---------- ② classify_failure：curl code=0 → env ----------

CURL_FAIL_TEXT = ("=========================== short test summary info =====\n"
                  "FAILED TestGenR1::test_get_ping - PHPUnit\\Framework\\ExpectationFailedException: "
                  "Failed asserting that 0 is identical to 200.\n"
                  "=========================== 0 passed, 15 failed, 0 skipped =====")


def test_classify_php_curl_code_zero_is_env():
    assert classify_failure(1, CURL_FAIL_TEXT, "") == "env", \
        "服务未起（curl code=0）必须归环境类，不得误收产品缺陷"


def test_classify_real_defect_still_product():
    """真实缺陷（actual=500）不受新特征误伤——0 只在请求未发生时出现。"""
    real = CURL_FAIL_TEXT.replace("asserting that 0 is identical to 200",
                                  "asserting that 500 is identical to 200")
    assert classify_failure(1, real, "") == "product_defect"


def test_decide_php_zero_pass_no_longer_stops_first_round():
    """端到端：php 15 败 code=0 → env → repair_env（不再是首轮 stop 疑似产品缺陷）。
    evaluate 层 exit 语义归一（php_exit_for_decide）只清 0 败形态，15 败保留
    exit≠0 → decide 走 classify=env → repair_env。"""
    verdict, kind, _ = decide(1, CURL_FAIL_TEXT, "", [], "fp1", 1, None, 0.7)
    assert verdict == "repair_env" and kind == "env", f"got {verdict}/{kind}"


# ---------- ③ extract_service_port：从生成用例解析目标端口 ----------

def test_extract_port_php(tmp_path: Path):
    (tmp_path / "TestGenR1.php").write_text(
        "$ch = curl_init('http://127.0.0.1:8080/ping');", encoding="utf-8")
    assert extract_service_port(tmp_path, "php") == 8080


def test_extract_port_java(tmp_path: Path):
    d = tmp_path / "src" / "test" / "java"
    d.mkdir(parents=True)
    (d / "TestGenR1.java").write_text(
        'HttpResponse<String> r = client.send(URI.create("http://localhost:9000/api"))',
        encoding="utf-8")
    assert extract_service_port(tmp_path, "java") == 9000


def test_extract_port_none_when_absent(tmp_path: Path):
    (tmp_path / "TestGenR1.php").write_text("<?php echo 1;", encoding="utf-8")
    assert extract_service_port(tmp_path, "php") is None
    assert extract_service_port(tmp_path, "python") is None   # 非目标语言恒 None


# ---------- ④ start_service_for_repair：触发判定与 fail-open ----------

class _FakeExec:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr
        self.ok = exit_code == 0


def _mk_ws_php(tmp_path: Path) -> Path:
    (tmp_path / "TestGenR1.php").write_text(
        "$ch = curl_init('http://127.0.0.1:8080/ping');", encoding="utf-8")
    (tmp_path / "index.php").write_text("<?php echo 'pong';", encoding="utf-8")
    return tmp_path


def test_repair_service_not_triggered_for_first_round(tmp_path):
    """首轮正常 provision（verdict≠repair_env）不起服务。"""
    import test_agent.graph.l2 as l2
    calls = []
    orig = l2._start_service_php
    l2._start_service_php = lambda *a: calls.append(a) or (True, "")
    try:
        out = start_service_for_repair(object(), object(), _mk_ws_php(tmp_path),
                                       "php", None, CURL_FAIL_TEXT)
    finally:
        l2._start_service_php = orig
    assert out is None and not calls


def test_repair_service_triggered_for_php(tmp_path, monkeypatch):
    """repair_env + 连接类失败 + 端口可解析 → 起 php 服务并留痕。"""
    import test_agent.graph.l2 as l2
    calls = []
    monkeypatch.setattr(l2, "_start_service_php",
                        lambda runner, container, port: calls.append(port) or (True, ""))
    out = start_service_for_repair(object(), object(), _mk_ws_php(tmp_path),
                                   "php", "repair_env", CURL_FAIL_TEXT)
    assert calls == [8080], "端口必须从用例解析（8080）"
    assert out and "已起被测服务" in out and "8080" in out


def test_repair_service_java_missing_main_class(tmp_path, monkeypatch):
    """java 连接失败但探测不到主类 → 跳过留痕（fail-open）。"""
    import test_agent.graph.l2 as l2
    d = tmp_path / "src" / "test" / "java"
    d.mkdir(parents=True)
    (d / "TestGenR1.java").write_text("http://localhost:9000/x", encoding="utf-8")
    monkeypatch.setattr(l2, "_detect_java_main", lambda ws: None)
    out = start_service_for_repair(object(), object(), tmp_path, "java",
                                   "repair_env", "java.net.ConnectException")
    assert out and "未探测到 Java 服务主类" in out


def test_repair_service_start_failure_fail_open(tmp_path, monkeypatch):
    """启动失败 → fail-open 留痕继续，不抛异常炸 provision。"""
    import test_agent.graph.l2 as l2
    monkeypatch.setattr(l2, "_start_service_php",
                        lambda *a: (False, "PHP 内置服务器未就绪"))
    out = start_service_for_repair(object(), object(), _mk_ws_php(tmp_path),
                                   "php", "repair_env", CURL_FAIL_TEXT)
    assert out and "服务启动失败" in out


def test_repair_service_not_triggered_for_non_connection_failure(tmp_path):
    """repair_env 但失败是别的环境错（如依赖装不上）→ 不起服务。"""
    out = start_service_for_repair(object(), object(), _mk_ws_php(tmp_path),
                                   "php", "repair_env", "composer install failed: network")
    assert out is None
