"""失败归因三闸 v1（11.4 恢复计划 §3 / Task G）：环境闸 / 差分闸 / flaky 连跑。

- 纯函数：2×2 判定矩阵、金丝雀判定、flaky 判定、差分执行器（fake exec 注入）
- 接线：run 节点金丝雀 fail-fast；evaluate 差分降级与 flaky 标注（旗标保护，默认关=旧行为）
- 研究先行：LIBRO 差分 / TestGen-LLM 连跑 / SWT-Bench 口径（见 attribution.py 模块头）
"""
import json
import types
from pathlib import Path

import pytest

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.skills import attribution
from test_agent.store import db


# ---------------- 纯函数 ----------------

def test_canary_cmd_supported_langs():
    for lang in ("python", "javascript"):
        cmd = attribution.canary_cmd(lang)
        assert cmd and cmd[-1].find(attribution.CANARY_OK) >= 0
    with pytest.raises(ValueError):
        attribution.canary_cmd("java")


def test_canary_ok_requires_marker_not_just_exit0():
    assert attribution.canary_ok(0, "CANARY_OK")
    assert not attribution.canary_ok(0, "silence")      # exit 0 但没跑出标记 → 假绿不通过
    assert not attribution.canary_ok(1, "CANARY_OK")


def test_classify_2x2_four_quadrants():
    # fail-to-pass → 高置信产品缺陷（LIBRO 语义）
    assert attribution.classify_2x2(True, False) == ("product_defect", "高")
    # 双败 → 排除产品缺陷；用例/环境不可分 → 低置信
    assert attribution.classify_2x2(True, True) == ("case_defect", "低")
    # bug 版过、对照版败 → 断言反演（把 bug 行为写成预期）→ 高置信用例错
    assert attribution.classify_2x2(False, True) == ("case_defect", "高")
    assert attribution.classify_2x2(False, False)[0] == "none"


def test_is_flaky_only_on_mixed_outcomes():
    assert attribution.is_flaky([True, False, True])
    assert not attribution.is_flaky([True, True])
    assert not attribution.is_flaky([False, False])


def test_subset_cmd_rejects_empty_and_unknown_lang():
    assert attribution.subset_cmd("python", ["a_test.py"])[:2] == ["python", "-m"]
    assert attribution.subset_cmd("javascript", ["a.test.js"])[0] == "npx"
    with pytest.raises(ValueError):
        attribution.subset_cmd("python", [])
    with pytest.raises(ValueError):
        attribution.subset_cmd("go", ["a_test.go"])


class _R:
    """exec 结果替身（duck-type sandbox.runner.ExecResult）。"""
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


def test_run_diff_gate_no_git_honest_unavailable():
    calls = []
    def exec_fn(cmd, timeout_s=30):
        calls.append(cmd)
        return _R(1, "", "fatal: not a git repository")
    out = attribution.run_diff_gate(exec_fn, "python", ["t_test.py"], "HEAD~1")
    assert out["status"] == "no_git" and out["label"] is None
    assert len(calls) == 1          # 第一道探测失败即停，不做任何 checkout


def test_run_diff_gate_no_ref_honest_unavailable():
    def exec_fn(cmd, timeout_s=30):
        if "--is-inside-work-tree" in cmd:
            return _R(0, "true\n", "")
        return _R(1, "", "unknown revision")
    out = attribution.run_diff_gate(exec_fn, "python", ["t_test.py"], "HEAD~1")
    assert out["status"] == "no_ref" and out["label"] is None


def _git_happy_exec(ref_fails: bool, restore_ok: bool = True, canary_ok: bool = True):
    """构造差分闸 happy-path 的容器执行脚本（按命令内容分流）。"""
    def exec_fn(cmd, timeout_s=30):
        joined = " ".join(cmd)
        if "--is-inside-work-tree" in joined:
            return _R(0, "true\n", "")
        if "^{commit}" in joined:
            return _R(0, "ref123\n", "")
        if joined.endswith("rev-parse HEAD"):
            return _R(0, "orig999\n", "")
        if "--detach" in joined:
            return _R(0, "", "")
        if joined.startswith("python -m pytest"):
            return _R(1 if ref_fails else 0,
                      "1 failed\n" if ref_fails else "1 passed\n", "")
        if "CANARY_OK" in joined:   # 双败格细分：金丝雀复跑（2026-11-05）
            return (_R(0, "CANARY_OK\n", "") if canary_ok
                    else _R(1, "", "pytest: not found"))
        if "checkout" in joined and "orig999" in joined:
            return _R(0 if restore_ok else 1, "", "conflict" if not restore_ok else "")
        raise AssertionError(f"未预期的命令: {joined}")
    return exec_fn


def test_run_diff_gate_ref_fails_downgrades_to_case_defect():
    out = attribution.run_diff_gate(_git_happy_exec(ref_fails=True), "python",
                                    ["test_gen_r1.py"], "HEAD~1")
    assert out["status"] == "ok" and out["ref_failed"] is True
    assert out["label"] == "case_defect" and out["confidence"] == "低"
    assert "金丝雀复跑通过" in out["detail"]       # 双败格细分证据在 detail


def test_run_diff_gate_double_fail_env_evidence_split():
    """双败格细分：环境特征错（ECONNREFUSED）→ evidence 指向环境；金丝雀挂 → 更强。"""
    out = attribution.run_diff_gate(
        _git_happy_exec(ref_fails=True, canary_ok=False), "python",
        ["test_gen_r1.py"], "HEAD~1",
        bug_stderr="connect ECONNREFUSED 127.0.0.1:3306")
    assert out["label"] == "case_defect"
    assert "环境特征错：ECONNREFUSED" in out["detail"]
    assert "金丝雀复跑未通过" in out["detail"]


def test_run_diff_gate_double_fail_case_evidence_split():
    """双败格细分：Cannot find module 特征 + 金丝雀健康 → evidence 指向用例错。"""
    out = attribution.run_diff_gate(
        _git_happy_exec(ref_fails=True), "python",
        ["test_gen_r1.py"], "HEAD~1",
        bug_stderr="ModuleNotFoundError: No module named 'mylib'")
    assert out["label"] == "case_defect"
    assert "用例特征错：ModuleNotFoundError" in out["detail"]
    assert "金丝雀复跑通过" in out["detail"]


def test_run_diff_gate_ref_passes_confirms_product_defect():
    out = attribution.run_diff_gate(_git_happy_exec(ref_fails=False), "python",
                                    ["test_gen_r1.py"], "HEAD~1")
    assert out["status"] == "ok" and out["ref_failed"] is False
    assert out["label"] == "product_defect" and out["confidence"] == "高"


def test_run_diff_gate_restore_failure_flagged_loudly():
    out = attribution.run_diff_gate(_git_happy_exec(ref_fails=False, restore_ok=False),
                                    "python", ["test_gen_r1.py"], "HEAD~1")
    assert out["status"] == "restore_failed"
    assert out["label"] is not None      # 结论仍有效，但状态要求人工检查


def test_outcome_passed_parses_py_and_js():
    assert attribution.outcome_passed("python", 0, "1 passed in 0.1s")
    assert not attribution.outcome_passed("python", 1, "1 failed, 1 passed")
    js = json.dumps({"numPassedTests": 3, "numFailedTests": 0})
    assert attribution.outcome_passed("javascript", 0, js)
    js_bad = json.dumps({"numPassedTests": 0, "numFailedTests": 2})
    assert not attribution.outcome_passed("javascript", 1, js_bad)


# ---------------- 接线（旗标保护） ----------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    yield c, tid, ses
    c.close()


class ScriptedRunner:
    """exec_in 按命令内容分流（容器替身）；client.containers.get 返回哑容器。"""
    def __init__(self, routes):
        self.routes = routes          # list[(predicate(cmd)->bool, () -> _R)]
        self.calls = []
        self.client = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda name: types.SimpleNamespace(name=name)))

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        self.calls.append(list(cmd))
        for pred, fn in self.routes:
            if pred(cmd):
                return fn()
        raise AssertionError(f"未预期的容器命令: {' '.join(cmd)}")


class OkRegistry:
    """run_tests 假注册表：返回一次全绿的执行输出。"""
    def __init__(self):
        self.invoked = 0
    def invoke(self, tid, skill, payload):
        self.invoked += 1
        return types.SimpleNamespace(status="ok",
                                     output=json.dumps({"exit": 0, "stdout": "1 passed",
                                                        "stderr": ""}, ensure_ascii=False))


def test_run_node_env_gate_fail_fast(env, monkeypatch):
    """金丝雀失败 → 任务 stop 判环境无效，真实用例一次都不跑（fail-fast）。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "ENV_GATE_ENABLED", True)
    runner = ScriptedRunner([
        (lambda cmd: cmd[:2] == ["python", "-c"], lambda: _R(1, "", "pytest: command not found"))])
    registry = OkRegistry()
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=registry,
                                 skills={"run_tests": types.SimpleNamespace()}))
    out = nodes["run"]({"task_id": tid, "session_id": ses, "primary_language": "python",
                        "container_name": "c", "workspace": str(config.DATA_DIR / "ws"),
                        "round_no": 1})
    assert out["verdict"] == "stop" and "环境闸" in out["stop_reason"]
    assert registry.invoked == 0        # 真实用例未执行
    assert any("CANARY_OK" in " ".join(cmd) or cmd[:2] == ["python", "-c"]
               for cmd in runner.calls)


def test_run_node_env_gate_off_skips_canary(env, monkeypatch):
    """旗标关（默认）：不执行金丝雀，行为与旧口径逐字节一致。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "ENV_GATE_ENABLED", False)
    runner = ScriptedRunner([
        (lambda cmd: cmd[:1] == ["cat"], lambda: _R(1, "", "no junit"))])  # junit cat 允许失败
    registry = OkRegistry()
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=registry,
                                 skills={"run_tests": types.SimpleNamespace()}))
    out = nodes["run"]({"task_id": tid, "session_id": ses, "primary_language": "python",
                        "container_name": "c", "workspace": str(config.DATA_DIR / "ws"),
                        "round_no": 1})
    assert registry.invoked == 1
    assert not any(cmd[:2] == ["python", "-c"] for cmd in runner.calls)  # 没跑过金丝雀
    assert "last_cmd" in out            # 供 flaky 连跑复用同一命令


FAIL_STDOUT = ("1 failed\n"
               "FAILED test_gen_r1.py::test_a - AssertionError: assert 1 == 2\n")


def _eval_state(tid, ses, **kw):
    base = {"task_id": tid, "session_id": ses, "primary_language": "python",
            "container_name": "c",
            "last_exit": 1, "last_stdout": FAIL_STDOUT, "last_stderr": "",
            "round_no": 1, "fingerprints": []}
    base.update(kw)
    return base


def test_evaluate_diff_gate_downgrades_when_ref_also_fails(env, monkeypatch):
    """差分闸：对照 ref 同样失败 → product_defect 降级 case_defect（低置信），如实写进轮次行。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "DIFF_GATE_ENABLED", True)
    runner = ScriptedRunner([
        (lambda cmd: "--is-inside-work-tree" in cmd, lambda: _R(0, "true\n", "")),
        (lambda cmd: any("^{commit}" in p for p in cmd), lambda: _R(0, "ref123\n", "")),
        (lambda cmd: cmd[-2:] == ["rev-parse", "HEAD"], lambda: _R(0, "orig999\n", "")),
        (lambda cmd: "--detach" in cmd, lambda: _R(0, "", "")),
        (lambda cmd: cmd[:3] == ["python", "-m", "pytest"], lambda: _R(1, "1 failed\n", "")),
        (lambda cmd: "CANARY_OK" in " ".join(cmd), lambda: _R(0, "CANARY_OK\n", "")),
        (lambda cmd: "checkout" in cmd and "orig999" in cmd, lambda: _R(0, "", "")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=None, skills={}))
    out = nodes["evaluate"](_eval_state(tid, ses))
    assert out["failure_kind"] == "case_defect"
    assert "差分闸降级" in out["stop_reason"]
    assert "金丝雀复跑通过" in out["stop_reason"]   # 双败格细分证据透传
    row = c.execute("SELECT failure_kind, stop_reason FROM evaluation_rounds "
                    "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert row["failure_kind"] == "case_defect"
    assert "差分闸" in row["stop_reason"]


def test_evaluate_diff_gate_keeps_label_when_ref_passes(env, monkeypatch):
    """差分闸：对照通过 → 维持高置信产品缺陷（fail-to-pass，不降级）。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "DIFF_GATE_ENABLED", True)
    runner = ScriptedRunner([
        (lambda cmd: "--is-inside-work-tree" in cmd, lambda: _R(0, "true\n", "")),
        (lambda cmd: any("^{commit}" in p for p in cmd), lambda: _R(0, "ref123\n", "")),
        (lambda cmd: cmd[-2:] == ["rev-parse", "HEAD"], lambda: _R(0, "orig999\n", "")),
        (lambda cmd: "--detach" in cmd, lambda: _R(0, "", "")),
        (lambda cmd: cmd[:3] == ["python", "-m", "pytest"], lambda: _R(0, "1 passed\n", "")),
        (lambda cmd: "checkout" in cmd and "orig999" in cmd, lambda: _R(0, "", "")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=None, skills={}))
    out = nodes["evaluate"](_eval_state(tid, ses))
    assert out["failure_kind"] == "product_defect"
    assert "差分闸" not in out["stop_reason"]


def test_evaluate_diff_gate_unavailable_keeps_heuristic(env, monkeypatch):
    """工作区非 git 仓库 → 差分闸如实不可用，维持启发式归因（不硬猜）。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "DIFF_GATE_ENABLED", True)
    runner = ScriptedRunner([
        (lambda cmd: "--is-inside-work-tree" in cmd, lambda: _R(1, "", "fatal: not a git repository"))])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=None, skills={}))
    out = nodes["evaluate"](_eval_state(tid, ses))
    assert out["failure_kind"] == "product_defect"      # 启发式原判维持
    assert "差分闸" not in out["stop_reason"]


def test_evaluate_flags_off_never_touches_container(env, monkeypatch):
    """默认全关：evaluate 不发任何容器命令，旧行为逐字节不变（499 基线保护）。"""
    c, tid, ses = env
    runner = ScriptedRunner([])          # 任何容器命令都会 AssertionError
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=None, skills={}))
    out = nodes["evaluate"](_eval_state(tid, ses))
    assert out["failure_kind"] == "product_defect"
    assert "flaky" not in out["stop_reason"]
    assert runner.calls == []


def test_evaluate_flaky_rerun_marks_reason(env, monkeypatch):
    """连跑 k 次 pass/fail 混合 → stop_reason 标 flaky（证据降级，不改 taxonomy）。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "FLAKY_RERUNS", 2)
    seq = iter([_R(0, "1 passed in 0.1s\n", ""), _R(1, "1 failed\n", "")])
    runner = ScriptedRunner([
        (lambda cmd: cmd[:3] == ["python", "-m", "pytest"], lambda: next(seq))])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=None, skills={}))
    out = nodes["evaluate"](_eval_state(tid, ses, last_cmd=["python", "-m", "pytest", "-q", "test_gen_r1.py"]))
    assert "flaky 连跑 2 次" in out["stop_reason"]


# ---------------- Task H：基准池 manifest 校验 + 体检卡 ----------------

from test_agent.skills.benchmarks import health_card, load_manifest, validate_manifest  # noqa: E402


def _base_manifest(**over):
    m = {"id": "py-demo", "language": "python", "track": "B",
         "repo": "https://example.com/demo", "official_test_cmd": "pytest -q",
         "truth_source": "none（B 轨）"}
    m.update(over)
    return m


def test_manifest_valid_minimal_b_track():
    assert validate_manifest(_base_manifest()) == []


def test_manifest_missing_required_fields():
    errs = validate_manifest({"id": "x"})
    assert any("language" in e for e in errs) and any("track" in e for e in errs)


def test_manifest_track_a_requires_diff_refs():
    errs = validate_manifest(_base_manifest(track="A"))
    assert any("buggy_ref" in e for e in errs) and any("fixed_ref" in e for e in errs)
    ok = validate_manifest(_base_manifest(track="A", buggy_ref="v1", fixed_ref="v2"))
    assert ok == []
    same = validate_manifest(_base_manifest(track="A", buggy_ref="v1", fixed_ref="v1"))
    assert any("不得相同" in e for e in same)


def test_manifest_unknown_lang_rejected():
    assert any("不支持的语言" in e
               for e in validate_manifest(_base_manifest(language="brainfuck")))


def test_load_manifest_missing_file_honest_error(tmp_path):
    m, errs = load_manifest(tmp_path / "nope.json")
    assert m is None and any("不存在" in e for e in errs)


def test_health_card_planned_vs_ok_vs_failed():
    assert health_card(_base_manifest())["status"] == "planned"     # 没体检≠通过
    assert health_card(_base_manifest(), build_ok=True, tests_ok=True)["status"] == "ok"
    assert health_card(_base_manifest(), build_ok=True, tests_ok=False)["status"] == "failed"


def test_example_manifest_in_repo_is_valid():
    example = (Path(__file__).parents[1] / "data" / "benchmarks" / "manifests"
               / "py-requests-smoke.example.json")
    m, errs = load_manifest(example)
    assert errs == [] and m["track"] == "B"


def test_env_checkup_script_end_to_end(tmp_path):
    """体检卡真跑：tmp 小项目官方测试全绿 → status=ok；退出码 0。"""
    import subprocess
    import sys
    checkout = tmp_path / "proj"
    checkout.mkdir()
    (checkout / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n",
                                         encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(_base_manifest(
        official_test_cmd=f'"{sys.executable}" -m pytest test_ok.py -q')), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "env_checkup.py"
    p = subprocess.run([sys.executable, str(script), str(manifest),
                        "--exec", "--checkout", str(checkout)],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    card = json.loads(p.stdout)
    assert card["status"] == "ok" and card["tests_ok"] is True


def test_env_checkup_script_rejects_invalid_manifest(tmp_path):
    import subprocess
    import sys
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "env_checkup.py"
    p = subprocess.run([sys.executable, str(script), str(bad)],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 2 and "manifest 校验失败" in p.stderr


# ---------- 生产接线（2026-11-05）：环境变量旗标 + 快照 keep_git ----------

def test_config_gate_flag_defaults_off_and_env_override(monkeypatch):
    """代码默认关（526 基线保护）；MTA_*=1 开启、0/false/off 关闭、未设=默认。"""
    assert config.ENV_GATE_ENABLED is False
    assert config.DIFF_GATE_ENABLED is False
    assert config.FLAKY_RERUNS == 0
    assert config._flag("MTA_TEST_UNSET", False) is False
    monkeypatch.setenv("MTA_TEST_FLAG", "1")
    assert config._flag("MTA_TEST_FLAG", False) is True
    monkeypatch.setenv("MTA_TEST_FLAG", "off")
    assert config._flag("MTA_TEST_FLAG", False) is False


def test_config_gate_flags_reload_from_env(monkeypatch):
    """环境变量在 config 导入时求值：MTA_ENV_GATE=1 + MTA_FLAKY_RERUNS=3 → reload 生效。"""
    import importlib
    from test_agent import config as cfg
    monkeypatch.setenv("MTA_ENV_GATE", "1")
    monkeypatch.setenv("MTA_FLAKY_RERUNS", "3")
    importlib.reload(cfg)
    try:
        assert cfg.ENV_GATE_ENABLED is True and cfg.FLAKY_RERUNS == 3
    finally:
        monkeypatch.delenv("MTA_ENV_GATE")
        monkeypatch.delenv("MTA_FLAKY_RERUNS")
        importlib.reload(cfg)          # 还原默认态，不污染其他测试
    assert cfg.ENV_GATE_ENABLED is False and cfg.FLAKY_RERUNS == 0


def test_snapshot_plan_keep_git_includes_dotgit(tmp_path):
    """缺口②：keep_git 路径=排除集合去掉 .git；默认路径仍剥（Task B 语义不变）。"""
    from test_agent.sandbox.runner import snapshot_plan
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    default = snapshot_plan(tmp_path)
    keep = snapshot_plan(tmp_path, exclude_dirs=config.SNAPSHOT_EXCLUDE_DIRS - {".git"})
    assert not any(".git" in str(p) for p in default["files"])
    assert any(".git" in str(p) for p in keep["files"])
