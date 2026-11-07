"""多语言分级（2026-11-01 用户拍板：Tier3 静态扫描兜底 + JS 执行链路 Tier2）：
扫描器（语言无关）/ jest→pytest 适配器 / analyze 分级名单 / gen_tests JS 形态 / 依赖探测。
铁律：pytest 不触碰真实库；LLM 不调用（纯函数+fake）。"""
import json
import re
from pathlib import Path

from test_agent import config
from test_agent.graph import nodes, repair
from test_agent.skills import gen_tests
from test_agent.skills.builtin import probe_deps_manifest
from test_agent.skills.js_adapter import jest_json_to_pytest_text
from test_agent.skills.prompt_library import build_user_prompt
from test_agent.skills.static_scan import parse_drafts, scan_workspace


# ---------- Tier3：语言无关扫描器 ----------

def test_scan_workspace_language_agnostic(tmp_path):
    ws = tmp_path / "p"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Calc.java").write_text(
        "public class Calc {\n"
        "  public int add(int a, int b) { return a + b; }\n"
        "  // TODO 除零待办\n"
        "  public int div(int a, int b) { try { return a / b; } catch (Exception e) { } return 0; }\n"
        "}\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("function noise() {}", encoding="utf-8")   # 噪声目录排除
    r = scan_workspace(ws)
    assert r["mode"] == "static_scan" and r["files"] == 1 and r["primary_language"] == "java"
    assert r["symbols"]["src/Calc.java"] == ["add", "div"]
    assert r["risk_count"] >= 2                                   # TODO + 空 catch
    assert any("空 catch" in l for l in r["risk_lines"])


def test_scan_workspace_empty(tmp_path):
    r = scan_workspace(tmp_path)
    assert r["files"] == 0 and r["symbols_total"] == 0 and r["risk_count"] == 0


def test_parse_drafts_fail_closed():
    ok = parse_drafts('{"drafts": [{"condition_id": 1, "name": "test_x", "expected": "返回 3"}]}')
    assert ok and ok[0]["name"] == "test_x"
    assert parse_drafts('{"drafts": [{"name": "no_expected"}]}') is None   # 缺预期=无 oracle
    assert parse_drafts("垃圾") is None


# ---------- 分级名单：java 仍静态（链路未接），typescript 升 Tier2（白包阶段1/2026-11-06） ----------

def test_tiering_contract():
    """full=python/javascript/typescript/java（白包阶段2 java 接线：surefire→java_adapter
    统一文本，下游零改动）；go/php/.NET 仍静态（白包阶段3/4/5 未接）。"""
    src = __import__("inspect").getsource(nodes.make_nodes)
    assert "strategy = _tier_strategy(primary)" in src
    assert nodes._tier_strategy("typescript") == "full"
    assert nodes._tier_strategy("java") == "full"
    assert nodes._tier_strategy("go") == "full"            # 白包阶段3 接线（go test -json→go_adapter）
    assert nodes._tier_strategy("php") == "static_only"
    assert config.EXEC_LANGUAGES["javascript"] == config.IMAGE_JS
    assert config.EXEC_LANGUAGES["typescript"] == config.IMAGE_JS
    assert config.IMAGE_JS in config.IMAGE_BUILD_CTX
    assert (config.IMAGE_BUILD_CTX[config.IMAGE_JS] / "Dockerfile").exists()


# ---------- JS 执行链路：jest→pytest 适配器对下游零改动 ----------

def _jest_json(passed=2, failed_assert=True, failed_ref=True):
    results = [{"title": "test_ok_等价类划分", "fullName": "s test_ok", "status": "passed"}]
    if failed_assert:
        results.append({"title": "test_div_zero_错误推测法", "fullName": "s x", "status": "failed",
                        "failureMessages": ["Error: expect(received).toThrow()\n\nReceived function did not throw"]})
    if failed_ref:
        results.append({"title": "test_missing", "fullName": "s y", "status": "failed",
                        "failureMessages": ["ReferenceError: foo is not defined"]})
    return json.dumps({
        "numPassedTests": passed, "numFailedTests": int(failed_assert) + int(failed_ref),
        "numPendingTests": 0, "numRuntimeErrorTestSuites": 0,
        "testResults": [{"name": "/work/test_gen_r1.test.js", "status": "failed",
                         "assertionResults": results}],
        "coverageMap": {"/work/src/calc.js": {
            "statementMap": {"0": {"start": {"line": 1}}, "1": {"start": {"line": 4}},
                             "2": {"start": {"line": 7}}, "3": {"start": {"line": 9}}},
            "s": {"0": 2, "1": 2, "2": 0, "3": 0}}}})


def test_jest_adapter_downstream_parsers():
    text = jest_json_to_pytest_text("PASS src\n" + _jest_json())
    assert re.search(r"(\d+) passed", text).group(1) == "2"
    assert re.search(r"(\d+) failed", text).group(1) == "2"
    assert re.search(r"TOTAL.*?\s(\d+)%", text).group(1) == "50"
    ids = repair.failing_nodeids(text)
    assert ids == ["test_gen_r1.test.js::test_div_zero_错误推测法", "test_gen_r1.test.js::test_missing"]
    # 错误类映射 → 归因三分类直接生效：expect 失败=证人，ReferenceError=用例自身坏（NameError）
    assert repair.error_class_of(text, ids[0]) == "AssertionError"
    assert repair.error_class_of(text, ids[1]) == "NameError"
    assert repair.assertion_failure_nodeids(text) == [ids[0]]
    assert "NameError" in repair.CASE_OWN_DEFECT_ERRORS                     # 可剪枝
    per = nodes._parse_per_file_coverage(text)
    assert per == [{"file": "src/calc.js", "stmts": 4, "miss": 2, "cover": 50, "missing": "7, 9"}]


def test_jest_adapter_non_json_passthrough():
    """无测试/配置炸时 jest 不出 JSON：原样透传让 evaluate 走 env 归因，不伪造计数。"""
    raw = "No tests found, exiting with code 1"
    assert jest_json_to_pytest_text(raw, "err") == raw + "\nerr"


def test_jest_adapter_suite_crash_to_error_line():
    data = json.dumps({"numPassedTests": 0, "numFailedTests": 0, "numPendingTests": 0,
                       "numRuntimeErrorTestSuites": 1,
                       "testResults": [{"name": "/work/test_gen_r1.test.js", "status": "failed",
                                        "assertionResults": [],
                                        "message": "Cannot find module './src/calc' from 'test_gen_r1.test.js'"}],
                       "coverageMap": {}})
    text = jest_json_to_pytest_text(data)
    assert "ERROR test_gen_r1.test.js - ImportError" in text
    assert "1 errors" in text


def test_build_run_cmd_js():
    assert nodes.build_run_cmd_js(1, ["test_gen_r1.test.js"]) == \
        ["jest", "--json", "--coverage", "--ci", "--silent", "--forceExit", "--testLocationInResults"]
    assert nodes.build_run_cmd_js(2, ["test_gen_r1.test.js"])[-1] == "test_gen_r1.test.js"


# ---------- gen_tests JS 形态 ----------

def test_prompt_js_output_contract():
    p = build_user_prompt([("src/calc.js", "function add(a,b){return a+b}")], "需求",
                          target_language="javascript")
    assert "jest" in p and 'test("test_xxx_等价类划分"' in p and "require(" in p
    assert "不含空格" in p
    p_py = build_user_prompt([("a.py", "x=1")], "需求")
    assert "jest" not in p_py and "def test_xxx" in p_py          # Python 口径逐字节不变


def test_parse_cases_accepts_jest_form():
    data = gen_tests._parse_cases(json.dumps({"imports": 'const {add}=require("./src/calc")',
        "cases": [{"name": "test_add_等价类划分", "code": 'test("test_add_等价类划分", () => { expect(add(1,2)).toBe(3); });'},
                  {"name": "bad", "code": "console.log(1)"}]}, ensure_ascii=False))
    assert data and len(data["cases"]) == 1 and gen_tests.JS_CASE_RE.search(data["cases"][0]["code"])


def test_static_filter_skips_js(tmp_path):
    cases = [{"name": "test_a", "code": 'test("test_a", () => { expect(1).toBe(1); });'}]
    kept, rejected = gen_tests._static_filter(cases, tmp_path)
    assert kept == cases and rejected == []


def test_collect_sources_js(tmp_path):
    ws = tmp_path / "js"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "calc.js").write_text("function add(a,b){return a+b}\nmodule.exports={add}", encoding="utf-8")
    (ws / "src" / "calc.test.js").write_text("test('x',()=>{})", encoding="utf-8")   # 自带测试=噪声
    (ws / "jest.config.js").write_text("module.exports={}", encoding="utf-8")        # 配置=噪声
    (ws / "test_gen_r1.test.js").write_text("test('g',()=>{})", encoding="utf-8")     # 生成物=噪声
    sources, skipped = gen_tests._collect_sources_detailed(ws, "javascript")
    assert [s[0] for s in sources] == ["src/calc.js"] and skipped == []
    assert gen_tests._collect_sources_detailed(ws, "python")[0] == []               # Python 口径不扫 js


def test_probe_deps_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert probe_deps_manifest(tmp_path) == "package_json"
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    assert probe_deps_manifest(tmp_path) == "requirements"                           # Python 清单优先


def test_static_scan_skill_importable_from_graph():
    """make_deps 注册 static_scan skill 的 import 路径（真机实锤：曾写成 graph 包内相对导入炸线程）。"""
    import inspect
    from test_agent.skills.static_scan import _static_scan_skill
    s = _static_scan_skill()
    assert s.name == "static_scan" and callable(s.func)
    assert "from ..skills.static_scan import _static_scan_skill" in inspect.getsource(nodes.make_deps)


def test_generate_static_drafts_end_to_end(tmp_path, monkeypatch):
    """草稿落库全流程（fake-LLM，tmp 库）：scan skill 留台账 + script(framework=static_draft) +
    cases 六要素落库——真机实锤 INSERT 绑定数错，这里走通整条 SQL。"""
    from types import SimpleNamespace
    from test_agent.skills import static_scan as ss
    from test_agent.store import db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id, confirmed) VALUES (1, ?, 1)", (uid,)).lastrowid
    conn.execute("INSERT INTO test_conditions(task_id, condition, priority) VALUES (?, 'add 两数相加', '高')", (tid,))
    conn.commit()
    ws = tmp_path / "ws"; (ws / "src").mkdir(parents=True)
    (ws / "src" / "Calc.java").write_text("public class Calc { public int add(int a,int b){return a+b;} }", encoding="utf-8")

    class FakeLLM:
        def invoke(self, messages):
            return SimpleNamespace(content=json.dumps({"drafts": [
                {"condition_id": 1, "name": "test_add_正常", "method": "等价类划分",
                 "input": "1,2", "steps": "调用 add(1,2)", "expected": "返回 3", "expected_basis": "需求明示"}]},
                ensure_ascii=False), usage_metadata=None)
    monkeypatch.setattr(ss, "make_chat", lambda **kw: FakeLLM())
    out = json.loads(ss.generate_static_drafts("k", tid, ws, "需求", conn))
    assert out["drafts"] == 1 and out["framework"] == "static_draft"
    row = conn.execute("SELECT framework, filename FROM test_scripts WHERE task_id=?", (tid,)).fetchone()
    assert row["framework"] == "static_draft" and row["filename"] == "static_draft.md"
    case = conn.execute("SELECT name, method, expected, expected_basis, content FROM test_cases").fetchone()
    assert case["name"] == "test_add_正常" and case["expected"] == "返回 3" and case["content"] == "add 两数相加"
    inv = conn.execute("SELECT COUNT(*) AS n FROM skill_invocations").fetchone()["n"]
    assert inv >= 1                                                # static_scan 留台账
    conn.close()
