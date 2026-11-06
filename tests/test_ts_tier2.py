"""白包阶段1·TypeScript 升级 Tier2（docs/多语言工具链接入任务包-20261106.md 阶段1）。

覆盖六个确定性位置的接线契约：
- 镜像映射：EXEC_LANGUAGES[typescript]=node20-jest
- 分级：typescript → full（analyze 策略纯函数）
- 渲染：生成物命名 .test.ts / 收集闸 tsc --noEmit（node --check 解析不了 TS 类型标注）
- 执行：散装内联 jest 配置含 ts-jest transform；run 节点 gen_files 含 .test.ts
- 工具链：install_deps 对缺 ts-jest 的 TS 项目 --no-save 补装判定
- 探测：stack_detect 认 tsconfig.json；js_adapter 兼容 ts-jest 输出（.ts 覆盖率键）
"""
import json

from test_agent import config


# ---------- 1) 镜像映射与分级 ----------

def test_exec_languages_typescript_maps_js_image():
    assert config.EXEC_LANGUAGES.get("typescript") == config.IMAGE_JS


def test_tier_strategy_typescript_full():
    from test_agent.graph.nodes import _tier_strategy
    assert _tier_strategy("typescript") == "full"
    assert _tier_strategy("javascript") == "full"
    assert _tier_strategy("python") == "full"
    assert _tier_strategy("java") == "full"             # 白包阶段2 接线（surefire→java_adapter）
    assert _tier_strategy("go") == "static_only"


# ---------- 2) 渲染层：命名与收集闸分流 ----------

def test_gen_file_naming_ts():
    from test_agent.skills.gen_tests import gen_file_naming
    assert gen_file_naming("typescript", 1) == ("test_gen_r1.test.ts", "jest")
    assert gen_file_naming("javascript", 2) == ("test_gen_r2.test.js", "jest")
    assert gen_file_naming("python", 3) == ("test_gen_r3.py", "pytest")
    assert gen_file_naming("typescript", 1, feedback=True, fb_no=2) == ("test_gen_fb2.test.ts", "jest")


def test_collect_gate_cmd_ts_uses_tsc():
    """TS 收集闸=tsc --noEmit 类型闸（node --check 解析不了类型标注——任务卡 S4 差异点）。"""
    from test_agent.skills.gen_tests import collect_gate_cmd
    ts_cmd = collect_gate_cmd("typescript", "test_gen_r1.test.ts")
    assert ts_cmd[0] == "tsc" and "--noEmit" in ts_cmd and ts_cmd[-1] == "test_gen_r1.test.ts"
    js_cmd = collect_gate_cmd("javascript", "test_gen_r1.test.js")
    assert js_cmd == ["node", "--check", "test_gen_r1.test.js"]
    py_cmd = collect_gate_cmd("python", "test_gen_r1.py")
    assert py_cmd[:2] == ["pytest", "--collect-only"]


def test_sut_manifest_excludes_test_ts():
    from test_agent.skills.gen_tests import _sut_manifest
    out = _sut_manifest([("src/calc.ts", "export function add(a: number, b: number) { return a + b; }"),
                         ("test_gen_r1.test.ts", 'test("test_x", () => {});'),
                         ("src/app.test.ts", 'test("y", () => {});')])
    assert out == ["src/calc.ts: add"]   # 测试生成物/自带测试不进被测函数清单（.test.ts 同口径）


# ---------- 3) 执行层：内联 jest 配置与 gen_files 收集 ----------

def test_inline_jest_config_with_ts_transform():
    from test_agent.graph.nodes import _inline_jest_config
    cfg = json.loads(_inline_jest_config(include_ts=True))
    assert any(m.endswith(".test.ts") for m in cfg["testMatch"])
    assert any(m.endswith(".test.js") for m in cfg["testMatch"])
    assert "ts-jest" in cfg["transform"]["\\.[jt]sx?$"][0]
    # 纯 JS 散装项目：不带 transform（与既有内联配置行为对齐，不为 TS 改 JS 路径）
    cfg_js = json.loads(_inline_jest_config(include_ts=False))
    assert "transform" not in cfg_js


def test_run_node_js_branch_accepts_typescript():
    """源码契约：run 节点的语言分支必须把 typescript 路由进 JS 执行路径（禁止另起炉灶）。
    源码契约测试先例：98f57af。"""
    from test_agent.graph import nodes
    import inspect
    mod_src = inspect.getsource(nodes)
    assert 'lang not in ("python", "javascript", "typescript", "java")' in mod_src
    assert 'lang in ("javascript", "typescript")' in mod_src


def test_pkg_declares_jest_and_injection_decision(tmp_path):
    """注入判定：package.json 内嵌 "jest" 键=有配置不注入；TS 项目有 package.json
    但无 jest 配置=仍注入（jest 默认配置无 transform 跑不动 .ts）。"""
    from test_agent.graph.nodes import _pkg_declares_jest
    assert _pkg_declares_jest(tmp_path) is False                       # 无 package.json
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    assert _pkg_declares_jest(tmp_path) is False
    (tmp_path / "package.json").write_text(
        '{"name": "x", "jest": {"testMatch": ["**/*.test.ts"]}}', encoding="utf-8")
    assert _pkg_declares_jest(tmp_path) is True
    # 源码契约：needs_inline 判定必须含 lang == "typescript" 分支（TS 加严）
    import inspect
    from test_agent.graph import nodes as nodes_mod
    src = inspect.getsource(nodes_mod)
    assert 'lang == "typescript" or not (ws / "package.json").exists()' in src


def test_build_run_cmd_js_accepts_ts_files():
    from test_agent.graph.nodes import build_run_cmd_js
    cmd = build_run_cmd_js(round_no=2, gen_files=["test_gen_r1.test.ts", "test_gen_r2.test.ts"])
    assert cmd[-2:] == ["test_gen_r1.test.ts", "test_gen_r2.test.ts"]


# ---------- 4) 工具链补装判定（install_deps 的 TS 救援） ----------

class _StubRes:
    def __init__(self, exit_code, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.exit_code == 0


class _StubRunner:
    def __init__(self, has_ts, has_tsjest):
        self._has_ts, self._has_tsjest = has_ts, has_tsjest

    def exec_in(self, container, cmd, **_kw):
        joined = " ".join(cmd)
        if "find /work" in joined and "-name '*.ts'" in joined:
            return _StubRes(0, "/work/src/calc.ts\n" if self._has_ts else "")
        if "test -d /work/node_modules/ts-jest" in joined:
            return _StubRes(0 if self._has_tsjest else 1)
        raise AssertionError(f"意外命令: {joined}")


def test_ts_tooling_needed_matrix():
    from test_agent.skills.builtin import _ts_tooling_needed
    assert _ts_tooling_needed(_StubRunner(True, False), object()) is True    # 有 .ts 且缺 ts-jest
    assert _ts_tooling_needed(_StubRunner(True, True), object()) is False   # 已有本地 ts-jest
    assert _ts_tooling_needed(_StubRunner(False, False), object()) is False # 纯 JS 项目不补


# ---------- 5) 探测与适配器 ----------

def test_stack_detect_tsconfig_feature(tmp_path):
    from test_agent.skills.stack_detect import detect_stack
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "calc.ts").write_text("export const x = 1;\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "tsconfig.json" in stack["manifests"]
    assert stack["language"] == "javascript"      # TS 归 node 工具链家族（同镜像）


def test_js_adapter_ts_jest_output_compat():
    """S5 兼容验证：ts-jest 输出仍是 jest JSON（transformer 对 CLI 输出透明），
    coverageMap 键为 .ts 源路径（istanbul sourceMap 回映射），下游按 .ts 呈现。"""
    from test_agent.skills.js_adapter import jest_json_to_pytest_text
    entry_pass = {"fullName": "test_add 等价类", "title": "test_add 等价类", "status": "passed"}
    entry_fail = {"fullName": "test_clamp 边界值", "title": "test_clamp 边界值",
                  "status": "failed", "failureMessages": ["Expected: 1 Received: 0"]}
    stmt_map = {str(i): {"start": {"line": i + 1}, "end": {"line": i + 1}} for i in range(3)}
    payload = {
        "numPassedTests": 1, "numFailedTests": 1, "success": False,
        "testResults": [
            {"name": "/work/test_gen_r1.test.ts", "status": "passed",
             "assertionResults": [entry_pass]},
            {"name": "/work/test_gen_r1.test.ts", "status": "failed",
             "assertionResults": [entry_fail],
             "message": "● test_clamp 边界值 › Expected: 1 Received: 0"},
        ],
        "coverageMap": {
            "/work/src/calc.ts": {"s": {"0": 1, "1": 0, "2": 1}, "statementMap": stmt_map}},
    }
    text = jest_json_to_pytest_text(json.dumps(payload), "")
    assert "1 passed" in text and "1 failed" in text
    assert "FAILED test_gen_r1.test.ts::test_clamp" in text
    assert "src/calc.ts" in text            # 覆盖率映射回 .ts 源（sourceMap 生效口径）


# ---------- 6) 两阶段重建：插入用例文件名按 TS 分流 ----------

def test_rebuild_imported_ts_naming(tmp_path):
    from test_agent.store import db
    from test_agent.skills.gen_tests import _rebuild_imported_file
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,'r','n','typescript')", (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,confirmed) "
                       "VALUES(?,?,1)", (pid, uid)).lastrowid
    sid = conn.execute("INSERT INTO test_scripts(task_id,framework,filename,content,"
                       "content_hash) VALUES(?,'jest','test_imported.test.ts','c','h')",
                       (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id,source,name,content,enabled) "
                 "VALUES(?,'imported','test_a','test(\"test_a\", () => {});',1)", (sid,))
    conn.commit()
    _rebuild_imported_file(conn, tid, tmp_path)
    assert (tmp_path / "test_imported.test.ts").exists()


# ---------- 4b) 工具链补装接线：散装 TS（无 package.json）也要补装 ----------
# 容器探针实锤（2026-11-07）：全局 ts-jest transformer 挂（npm 全局布局嵌套——ts-jest 依赖的
# jest-util 在 jest 包内嵌套 node_modules，NODE_PATH 顶层解析不到，Cannot find module 'jest-util'）；
# 本地 node_modules 才有完整依赖树（exp1/exp3 双实测：带清单与散装补装后 jest 均跑通，
# coverageMap 键回映射 .ts）。npm install --no-save 在无 package.json 目录实测 exit 0 可用。


class _TsToolingStubRunner:
    """记录命令并按形态应答：find .ts 命中 / 本地缺 ts-jest / 其余成功。"""

    def __init__(self):
        self.calls = []

    @property
    def client(self):
        return type("C", (), {"containers": type("Cs", (), {
            "get": staticmethod(lambda name: object())})()})()

    def exec_in(self, container, cmd, workdir="/work", timeout_s=120):
        self.calls.append((list(cmd), timeout_s))
        joined = " ".join(cmd)
        if "find /work" in joined and "-name '*.ts'" in joined:
            return _StubRes(0, "/work/src/calc.ts\n")
        if "test -d /work/node_modules/ts-jest" in joined:
            return _StubRes(1)
        return _StubRes(0, "ok")


def test_probe_loose_ts_returns_ts_loose(tmp_path):
    """散装 TS 判定：无任何清单但有 .ts 源 → probe 返回 'ts_loose'（≠None，不改
    「真无清单=零执行」既有契约）；带 package.json 的 TS 项目仍优先 package_json。"""
    from test_agent.skills.builtin import probe_deps_manifest
    assert probe_deps_manifest(tmp_path) is None                    # 空目录
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.ts").write_text("export const x = 1;\n", encoding="utf-8")
    assert probe_deps_manifest(tmp_path) == "ts_loose"
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    assert probe_deps_manifest(tmp_path) == "package_json"          # 清单优先于 ts_loose


def test_probe_skips_node_modules_ts(tmp_path):
    """node_modules 里的 .d.ts 不是散装 TS 信号（SKIP_DIRS 剪枝）。"""
    from test_agent.skills.builtin import probe_deps_manifest
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.d.ts").write_text("export {};\n", encoding="utf-8")
    assert probe_deps_manifest(tmp_path) is None


def test_install_deps_loose_ts_still_installs_tooling():
    """散装 TS（probe 判 ts_loose）触发 ts-jest 补装。"""
    from test_agent.skills.builtin import build_skills
    r = _TsToolingStubRunner()
    out = build_skills(r)["install_deps"].func(1, {"container_name": "mta-run-1",
                                                   "deps_manifest": "ts_loose"})
    joined = [" ".join(c[0]) for c in r.calls]
    assert any("npm" in j and "ts-jest@29" in j for j in joined), out


def test_install_deps_python_manifests_do_not_probe_ts():
    """requirements/pyproject/setup_py 分支不触发 TS 探测（既有命令序列契约保持）。"""
    from test_agent.skills.builtin import build_skills

    class _Spy(_TsToolingStubRunner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=120):
            self.calls.append((list(cmd), timeout_s))
            return _StubRes(0, "ok")

    for manifest in ("requirements", "pyproject", "setup_py"):
        r = _Spy()
        build_skills(r)["install_deps"].func(1, {"container_name": "mta-run-1",
                                                 "deps_manifest": manifest})
        assert not any("find /work" in " ".join(c[0]) for c in r.calls), manifest


def test_install_deps_loose_js_skips_tooling():
    """散装目录只有 .js（无 .ts）→ probe 不判 ts_loose → install_deps 零补装。"""
    from test_agent.skills.builtin import build_skills

    class _JsOnly(_TsToolingStubRunner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=120):
            joined = " ".join(cmd)
            if "find /work" in joined and "-name '*.ts'" in joined:
                return _StubRes(0, "")
            self.calls.append((list(cmd), timeout_s))
            return _StubRes(0, "ok")

    r = _JsOnly()
    build_skills(r)["install_deps"].func(1, {"container_name": "mta-run-1"})
    assert not any("ts-jest@29" in " ".join(c[0]) for c in r.calls)


# ---------- 7) 解析层语言形态闸：混写 pytest 的用例在解析层剔除 ----------
# ts-calc 真机实锤（2026-11-07，废案留档 data/reports/collect_gate_reject/）：LLM 把
# 五方法知识库的 Python 示例照抄进 code（import pytest/@pytest.mark.parametrize/def
# test_/assert），旧过滤条件「def test_ 或 JS_CASE_RE」放行混写 → 混进 .test.ts 被
# tsc 闸整案废案（三轮全灭）。JS/TS 解析层只认 jest 形态；pytest 槽位模板（渲染出
# def test_）同样不适用。语言差异合法位置=渲染层（任务卡纪律）。


def test_parse_cases_ts_drops_python_contaminated():
    """TS 项目：混写 pytest 形态的用例在解析层剔除，jest 形态保留。"""
    from test_agent.skills.gen_tests import _parse_cases
    resp = ('{"imports": "import { add } from \'./src/calc\';", "cases": ['
            '{"name": "test_a", "code": "test(\\"test_a\\", () => { expect(add(1, 1)).toBe(2); });"},'
            '{"name": "test_bad", "code": "def test_bad():\\n    assert add(1, 1) == 2"},'
            '{"name": "test_param", "code": "@pytest.mark.parametrize(\'v\', [1])\\n'
            'def test_param(v):\\n    assert add(v, 0) == v"}]}')
    data = _parse_cases(resp, lang="typescript")
    assert data is not None
    assert [c["name"] for c in data["cases"]] == ["test_a"]


def test_parse_cases_js_drops_python_contaminated():
    """JS 项目同口径：def test_ 形态混写剔除（既有漏洞一并修复）。"""
    from test_agent.skills.gen_tests import _parse_cases
    resp = ('{"cases": ['
            '{"name": "test_ok", "code": "test(\\"test_ok\\", () => {});"},'
            '{"name": "test_py", "code": "def test_py():\\n    assert True"}]}')
    data = _parse_cases(resp, lang="javascript")
    assert [c["name"] for c in data["cases"]] == ["test_ok"]


def test_parse_cases_python_keeps_def_test():
    """Python 项目口径不变：def test_ 形态合法（既有行为回归保护）。"""
    from test_agent.skills.gen_tests import _parse_cases
    resp = '{"cases": [{"name": "test_py", "code": "def test_py():\\n    assert True"}]}'
    data = _parse_cases(resp, lang="python")
    assert data is not None and [c["name"] for c in data["cases"]] == ["test_py"]
    assert _parse_cases(resp) is not None            # 无 lang（老调用方）同 Python 口径


def test_parse_cases_ts_drops_pytest_slot_templates():
    """pytest 槽位模板（渲染 def test_）对 TS 不适用：剔除后 cleaned 空 → None（诚实重试）。"""
    from test_agent.skills.gen_tests import _parse_cases
    resp = ('{"cases": [{"template": "T2", "slots": {"target": "clamp", "value": "1"}}]}')
    assert _parse_cases(resp, lang="typescript") is None


def test_pkg_declares_jest_not_fooled_by_scripts_value(tmp_path):
    """scripts 值里的 "test": "jest" 不是 jest 配置——必须 JSON 解析顶层键判定
    （ts-calc 真机实锤：子串误命中 → 不注入内联配置 → 全局 jest 无配置拒跑）。"""
    from test_agent.graph.nodes import _pkg_declares_jest
    (tmp_path / "package.json").write_text(
        '{"name": "ts-calc-demo", "scripts": {"test": "jest"}}', encoding="utf-8")
    assert _pkg_declares_jest(tmp_path) is False
    (tmp_path / "package.json").write_text(
        '{"name": "x", "jest": {"testEnvironment": "node"}}', encoding="utf-8")
    assert _pkg_declares_jest(tmp_path) is True
    (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
    assert _pkg_declares_jest(tmp_path) is False


# 注（2026-11-07 返工第 3 轮定案）：per_file「只收有缺口文件行」是 B2 缺口回灌的设计
# 口径（tests/test_prune.py 守卫；evaluate.hotspot_coverage 依赖「不在 per_file=已覆盖」
# 正向证据规则）——100% 全覆盖文件不进 per_file 是语义不是 bug；TS「覆盖率映射回 .ts」
# 验收走容器臂直接验 coverageMap 键（scripts/smoke_ts.py verify_sourcemap），不走 per_file。
