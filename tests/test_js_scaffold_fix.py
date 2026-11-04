"""JS 路径脚手架修正 + jest stderr 诊断段 + 归因判别纪律（task_366 误诊修复轮，2026-11-04）。

实锤链：平台把用例文件写在仓库根（__dirname 即仓库根），LLM 按 jest 惯例写
path.join(__dirname, '..')（假设用例在 test/ 子目录）→ 容器内 ROOT='/' → 12 条用例
全部 "Cannot find module '/xxx.js'"（exit 1 + stdout 空）→ 证据只在 jest stderr 且
被 adapter 成功解析路径丢弃 → 全部被误归因"疑似产品缺陷（并发）"。
修复三层：生成侧确定性修正（本文件前半）+ stderr 诊断段不再蒸发（中段）+
defect_analysis 归因判别纪律（尾段）。
"""
from test_agent.skills.defect_analysis import build_analysis_prompt
from test_agent.skills.gen_tests import _fix_js_scaffold_paths
from test_agent.skills.js_adapter import jest_json_to_pytest_text, stderr_diagnostics
from test_agent.skills.prompt_library import build_user_prompt


# ---------- 生成侧：路径脚手架确定性修正 ----------

def test_fix_single_join_dotdot():
    fixed, n = _fix_js_scaffold_paths("const ROOT = path.join(__dirname, '..');")
    assert fixed == "const ROOT = __dirname;" and n == 1


def test_fix_single_resolve_dotdot_double_quotes():
    fixed, n = _fix_js_scaffold_paths('const ROOT = path.resolve(__dirname, "..")')
    assert fixed == "const ROOT = __dirname" and n == 1


def test_fix_multi_segment_join():
    fixed, n = _fix_js_scaffold_paths(
        "const P = path.join(__dirname, '..', 'lib', 'x.js');")
    assert fixed == "const P = path.join(__dirname, 'lib', 'x.js');" and n == 1


def test_normal_sibling_reference_untouched():
    src = "const A = path.join(__dirname, 'apicheck.js');"
    fixed, n = _fix_js_scaffold_paths(src)
    assert fixed == src and n == 0


def test_two_levels_up_untouched():
    # 上跳两级无歧义修正语义（可能是 monorepo 意图），明确不在处理范围——
    # 由提示词布局纪律防其出现，真出现时 stderr 诊断段可见
    src = "const P = path.join(__dirname, '..', '..');"
    fixed, n = _fix_js_scaffold_paths(src)
    assert fixed == src and n == 0


def test_task366_real_sample():
    # task_366 用例文件的实锤形态：ROOT 定义在文件头，12 条用例共用
    src = ("const path = require('path');\n"
           "const ROOT = path.join(__dirname, '..');\n"
           "const APICHECK = path.join(ROOT, 'apicheck.js');\n")
    fixed, n = _fix_js_scaffold_paths(src)
    assert n == 1
    assert "const ROOT = __dirname;" in fixed
    assert "path.join(ROOT, 'apicheck.js')" in fixed    # 修正不越界：后续引用原样


# ---------- 归因侧：stderr 诊断段 ----------

def test_stderr_diagnostics_dedup_with_count():
    err = ("Error: Cannot find module '/apicheck.js'\nRequire stack:\n- /work\n"
           "Error: Cannot find module '/apicheck.js'\n"
           "Error: Cannot find module '/cleandata.js'\n")
    diag = stderr_diagnostics(err)
    assert "Cannot find module '/apicheck.js' × 2" in diag
    assert "Cannot find module '/cleandata.js'" in diag
    assert "Require stack" not in diag                  # 堆栈行不进诊断


def test_stderr_diagnostics_empty_on_no_match():
    assert stderr_diagnostics("PASS test.js\n") == ""
    assert stderr_diagnostics("") == ""


def _jest_json(failed=1, passed=0):
    return ('{"numPassedTests": %d, "numFailedTests": %d, "numPendingTests": 0,'
            ' "numRuntimeErrorTestSuites": 0, "coverageMap": {},'
            ' "testResults": [{"name": "/work/t.test.js", "status": "failed",'
            ' "assertionResults": [{"status": "failed", "title": "test_a",'
            ' "fullName": "test_a", "failureMessages":'
            ' ["Error: expect(received).toBe(expected)\\nExpected: 0\\nReceived: 1"]}]}]}'
            % (passed, failed))


def test_adapter_keeps_stderr_diagnostics_on_success_path():
    out = jest_json_to_pytest_text(_jest_json(),
                                   "Error: Cannot find module '/apicheck.js'\n")
    assert "stderr diagnostics (jest)" in out
    assert "Cannot find module '/apicheck.js'" in out
    assert "1 failed" in out


def test_adapter_diag_section_does_not_pollute_counts():
    # 计数正则安全性：诊断段措辞不得含 "N failed/errors" 字样（evaluate 靠它数数）
    out = jest_json_to_pytest_text(_jest_json(failed=12),
                                   "Error: Cannot find module '/x.js' × 12\n"
                                   "ECONNREFUSED 127.0.0.1:3306\n")
    assert "12 failed" in out and "Cannot find module" in out
    assert "ECONNREFUSED" in out


def test_adapter_non_json_path_unchanged():
    # 解析失败路径原样拼接 stdout+stderr（既有语义不变）
    out = jest_json_to_pytest_text("not json at all", "boom")
    assert "not json" in out and "boom" in out


# ---------- 归因判别纪律：提示词层 ----------

def test_analysis_prompt_has_discrimination_rules():
    evidence = {"failed": [{"name": "test_a", "method": "等价类划分",
                            "expected_basis": "源码推导", "expected": "code 0",
                            "input": "", "error_class": "AssertionError",
                            "message": "Expected: 0 Received: 1", "code": "test('t')"}],
                "requirement": "并发受得了", "failure_kind": "product_defect",
                "stop_reason": "疑似产品缺陷"}
    p = build_analysis_prompt(evidence, "function f(){}")
    assert "归因判别纪律" in p
    assert "Cannot find module" in p and "ECONNREFUSED" in p
    assert "不构成产品缺陷证据" in p
    assert "需人工查看执行 stderr" in p


def test_user_prompt_js_layout_contract():
    p = build_user_prompt([("src/calc.js", "function add(a,b){return a+b}")], "需求",
                          target_language="javascript")
    assert "__dirname 即项目根" in p
    assert "禁止写" in p and "path.join(__dirname, \"..\")" in p.replace("**", "")
    assert "Object.assign({}, process.env" in p
    # Python 分支：布局纪律同在，但不含 jest 字样（口径互不污染）
    p_py = build_user_prompt([("a.py", "x=1")], "需求", target_language="python")
    assert "执行布局" in p_py and "os.path.dirname" in p_py
