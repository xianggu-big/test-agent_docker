"""白包阶段3·Go 接线测试矩阵（docs/多语言工具链接入任务包-20261106.md 阶段3）。

单测锁六位置的闸门与解析器（改坏实现必须变红）；事件流/coverprofile/panic 形态
全部来自容器探针实锤（2026-11-07，golang:1.22 真跑）。真机全链路=scripts/smoke_go.py。
"""
import re

from test_agent.graph.nodes import _tier_strategy, build_run_cmd_go
from test_agent.graph.repair import classify_failure
from test_agent.skills.attribution import canary_cmd
from test_agent.skills.gen_tests import (
    GO_CASE_RE,
    _go_package_of,
    _normalize_go_imports,
    collect_gate_cmd,
    gen_file_naming,
)


# —— S4 渲染层形态闸（解析层：pytest/jest/JUnit 混写必须被剔除） ——

def test_go_case_re_matches_go_form():
    assert GO_CASE_RE.search("func TestAdd_等价类划分(t *testing.T) {")
    assert GO_CASE_RE.search("func  TestXxx( t  *testing.T ) {")


def test_go_case_re_rejects_foreign_forms():
    assert not GO_CASE_RE.search("def test_xxx():\n    assert add(1,1)==2")
    assert not GO_CASE_RE.search('test("test_xxx", () => { expect(1).toBe(1) })')
    assert not GO_CASE_RE.search("@Test\npublic void test_xxx() {}")
    assert not GO_CASE_RE.search("func helper(t *testing.T) {}")   # 非 Test 前缀
    assert not GO_CASE_RE.search("func TestXxx(t *testing.M) {}")  # 非 *testing.T


def test_parse_cases_go_filters_pytest_form():
    from test_agent.skills.gen_tests import _parse_cases
    data = {"cases": [
        {"name": "t1", "code": "func TestAdd(t *testing.T) {\n\tt.Errorf(\"x\")\n}"},
        {"name": "t2", "code": "def test_add():\n    assert True"},
        {"name": "t3", "code": "test(\"test_x\", () => {})"},
    ]}
    out = _parse_cases(re.sub(r"\s+", " ", __import__("json").dumps(data)), lang="go")
    assert out is not None and [c["name"] for c in out["cases"]] == ["t1"]


# —— S3 命名/闸命令 ——

def test_gen_file_naming_go():
    assert gen_file_naming("go", 1) == ("TestGenR1_test.go", "go_test")
    assert gen_file_naming("go", 3, feedback=True, fb_no=2) == ("TestGenFb2_test.go", "go_test")


def test_collect_gate_cmd_go():
    cmd = collect_gate_cmd("go", "TestGenR1_test.go")
    assert cmd == ["sh", "-c", "go build ./... && go test -c -o /tmp/mta_gate.bin ."]


# —— 渲染辅助（包壳） ——

def test_go_package_of(tmp_path):
    (tmp_path / "order.go").write_text("package shop\n\nfunc GetTier(n int) string {\n\treturn \"A\"\n}\n",
                                       encoding="utf-8")
    (tmp_path / "x_test.go").write_text("package wrongpkg\n", encoding="utf-8")
    assert _go_package_of(tmp_path) == "shop"
    assert _go_package_of(tmp_path / "nonexist") == "main"


def test_normalize_go_imports():
    assert _normalize_go_imports('import "testing"\nimport "strings"') == ['"testing"', '"strings"']
    assert _normalize_go_imports('testing\nstrings') == ['"testing"', '"strings"']  # 裸路径补引号
    assert _normalize_go_imports("") == ['"testing"']                               # 最小回退
    assert _normalize_go_imports('import (\n"testing"\n)') == ['"testing"']


# —— S5 go_adapter（探针实锤形态回放） ——

GOJSON = (
    '{"Action":"start","Package":"probe"}\n'
    '{"Action":"run","Package":"probe","Test":"TestAdd"}\n'
    '{"Action":"output","Package":"probe","Test":"TestAdd","Output":"=== RUN   TestAdd\\n"}\n'
    '{"Action":"output","Package":"probe","Test":"TestAdd",'
    '"Output":"    probe_test.go:10: Add(40,2)=43 want 42\\n"}\n'
    '{"Action":"output","Package":"probe","Test":"TestAdd","Output":"--- FAIL: TestAdd (0.00s)\\n"}\n'
    '{"Action":"fail","Package":"probe","Test":"TestAdd","Elapsed":0.008}\n'
    '{"Action":"pass","Package":"probe","Test":"TestOk","Elapsed":0.001}\n'
    '{"Action":"output","Package":"probe","Output":"FAIL\\tprobe\\t0.006s\\n"}\n'
    '{"Action":"fail","Package":"probe","Elapsed":0.008}\n'
)

BUILDFAIL = (
    '{"Action":"output","Package":"probe","Output":"# probe [probe.test]\\n"}\n'
    '{"Action":"output","Package":"probe","Output":"./broken_test.go:5:2: undefined: undefinedHelper\\n"}\n'
    '{"Action":"output","Package":"probe","Output":"FAIL\\tprobe [build failed]\\n"}\n'
    '{"Action":"fail","Package":"probe","Elapsed":0}\n'
)


def test_go_adapter_text_counts_and_failed_lines():
    text = __import__("test_agent.skills.go_adapter", fromlist=["x"]) \
        .go_test_json_to_pytest_text(GOJSON)
    assert "1 passed, 1 failed" in text
    assert "FAILED probe::TestAdd - probe_test.go:10: Add(40,2)=43 want 42" in text
    assert re.search(r"TOTAL.*?\s(\d+)%", text) is None or "coverage" not in text


def test_go_adapter_build_failed_error_row():
    mod = __import__("test_agent.skills.go_adapter", fromlist=["x"])
    text = mod.go_test_json_to_pytest_text(BUILDFAIL)
    assert "ERROR probe::[build failed]" in text
    assert "undefined: undefinedHelper" in text
    assert "0 passed, 1 failed" in text
    rows = mod.go_test_json_to_rows(BUILDFAIL)
    assert rows and rows[0]["nodeid"] == "probe::[build failed]" and rows[0]["status"] == "error"


def test_go_adapter_rows_contract():
    mod = __import__("test_agent.skills.go_adapter", fromlist=["x"])
    rows = mod.go_test_json_to_rows(GOJSON)
    by = {r["nodeid"]: r for r in rows}
    assert by["probe::TestAdd"]["status"] == "failed"
    assert by["probe::TestAdd"]["duration_ms"] == 8
    assert by["probe::TestOk"]["status"] == "passed"


def test_go_adapter_panic_section_preserved():
    mod = __import__("test_agent.skills.go_adapter", fromlist=["x"])
    panic_json = (
        '{"Action":"output","Package":"probe","Test":"TestBoom",'
        '"Output":"panic: runtime error: index out of range [1] with length 0\\n"}\n'
        '{"Action":"output","Package":"probe","Test":"TestBoom",'
        '"Output":"goroutine 6 [running]:\\n"}\n'
        '{"Action":"output","Package":"probe","Test":"TestBoom",'
        '"Output":"\\t/work/boom.go:4 +0x1a\\n"}\n'
        '{"Action":"fail","Package":"probe","Test":"TestBoom","Elapsed":0.01}\n'
    )
    text = mod.go_test_json_to_pytest_text(panic_json)
    assert "go panic (top frames)" in text
    assert "/work/boom.go:4" in text


COVER = "mode: set\n" \
        "probe/order.go:3.24,4.23 1 1\n" \
        "probe/order.go:4.23,6.3 2 0\n" \
        "probe/order.go:7.2,7.14 1 1\n" \
        "probe/full.go:1.1,2.2 1 1\n"


def test_coverprofile_rows_gaps_only():
    mod = __import__("test_agent.skills.go_adapter", fromlist=["x"])
    rows, total = mod.coverprofile_to_rows(COVER)
    # B2 口径：全覆盖文件不进行列表（full.go 缺席）
    assert [r["file"] for r in rows] == ["probe/order.go"]
    assert rows[0]["miss"] == 2 and rows[0]["cover"] == 2
    assert rows[0]["missing"] == [4]
    assert total == 60   # 3 covered / 5 stmts（order.go 2/4 + full.go 1/1）


# —— S6 归因：panic 帧归属（classify_failure go 分支） ——

def test_classify_panic_sut_frame_is_product_defect():
    text = ("FAILED probe::TestBoom - panic: runtime error: index out of range\n"
            "---------- go panic (top frames) ----------\n"
            "panic: runtime error: index out of range\n"
            "goroutine 6 [running]:\n"
            "\t/usr/local/go/src/testing/testing.go:1595 +0x2c8\n"
            "\tprobe.Boom(...)\n"
            "\t\t/work/boom.go:4\n")
    assert classify_failure(1, text, "") == "product_defect"


def test_classify_panic_test_frame_is_case_defect():
    text = ("panic: runtime error: invalid memory address\n"
            "goroutine 6 [running]:\n"
            "\t/usr/local/go/src/runtime/panic.go:770\n"
            "\tprobe.TestBoom(...)\n"
            "\t\t/work/TestGenR1_test.go:8\n")
    assert classify_failure(1, text, "") == "case_defect"


def test_classify_panic_stdlib_only_falls_product():
    # 全 stdlib 帧（无法定位）→ 默认疑似产品缺陷（panic 仍来自被测进程）
    text = "panic: runtime error\ngoroutine 1 [running]:\n\t/usr/local/go/src/runtime/panic.go:770\n"
    assert classify_failure(1, text, "") == "product_defect"


def test_classify_non_go_text_unchanged():
    # 触发词 panic: 为 Go 专属——python/jest 文本既有归类零变化
    assert classify_failure(1, "FAILED tests/test_x.py::test_a - AssertionError: 1 != 2", "") \
        == "product_defect"
    assert classify_failure(1, "FAIL src/x.test.js\n● ReferenceError: y is not defined\n"
                               "    at evalmachineWithContext.js:1:1", "") == "case_defect"


# —— 执行命令/分级/金丝雀 ——

def test_build_run_cmd_go():
    assert build_run_cmd_go() == ["sh", "-c",
                                  "go test -json -coverprofile=/tmp/go_cover.out ./... 2>&1"]


def test_tier_strategy_go_full():
    assert _tier_strategy("go") == "full"


def test_canary_cmd_go_shape():
    cmd = canary_cmd("go")
    assert cmd[0] == "sh" and "go version" in cmd[2] and "/tmp/_canary.tmp" in cmd[2]
