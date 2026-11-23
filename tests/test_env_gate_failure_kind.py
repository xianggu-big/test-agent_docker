"""任务卡§2（2026-11-23 晚）：环境闸拦停的 failure_kind 如实填。

现象：#2597（php-collection 无 composer.lock 被平台闸拦停）报告行 failure_kind=None
——前端 35cee83 已兜底渲染失败卡，但根上是闸 stop 时没把 kind 带进 state，判决卡
拿不到 FK_META["env"] 的灰卡+环境建议。改法：四个环境类闸 stop（provision 镜像
未就绪 / Go 工具链闸 / PHP 平台闸 / run 金丝雀环境闸）返回体补 failure_kind="env"
（取值域与 classify_failure 一致，前端 FK_META 已有 env 键——任务卡原文写
env_error 系笔误，对码后按真实键名修正）。非闸路径逐字节不变；历史行不回填。
"""
import json
import types
from pathlib import Path

import pytest

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.store import db


class _R:
    """exec 结果替身（duck-type sandbox.runner.ExecResult）。"""

    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class GateRunner:
    """provision/run 驱动替身：exec_in 按命令分流 + 容器/镜像/快照哑实现。"""

    def __init__(self, routes, image_ok=True):
        self.routes = routes          # list[(predicate(cmd)->bool, () -> _R)]
        self.image_ok = image_ok
        self.calls = []
        self.client = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda name: types.SimpleNamespace(name=name)))

    def image_exists(self, image):
        return self.image_ok

    def ensure_image(self, image):
        return self.image_ok

    def ensure_container(self, image, name, fresh=False, volumes=None):
        return types.SimpleNamespace(id="cid", name=name)

    def copy_dir_into(self, container, src, keep_git=False):
        return {"file_count": 1, "total_bytes": 10, "excluded_dirs": [], "skipped_files": []}

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for pred, make in self.routes:
            if pred(cmd, joined):
                return make()
        return _R(0, "", "")          # 兜底：预热等旁路命令一律成功


class OkRegistry:
    def invoke(self, tid, skill, payload):
        return types.SimpleNamespace(status="ok",
                                     output=json.dumps({"exit": 0, "stdout": "", "stderr": ""}))


PHP_PLATFORM_FAIL = """composer/composer  requires PHP >= 8.4.1
php-64bit          8.3.36  failed
"""


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


def _state(c, tid, ses, ws, lang, image="mta-test:php83"):
    return {"task_id": tid, "session_id": ses, "primary_language": lang,
            "container_name": "c", "workspace": str(ws),
            "image": image, "round_no": 1}


def test_provision_php_gate_stop_sets_env_kind_then_report_row(env, tmp_path):
    """PHP 平台闸拦停 → state 带 failure_kind=env → report 行 defects JSON 如实落库。"""
    c, tid, ses = env
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "composer.json").write_text('{"require": {"php": "^8.1"}}', encoding="utf-8")
    runner = GateRunner([
        (lambda cmd, j: "check-platform-reqs" in j, lambda: _R(2, PHP_PLATFORM_FAIL, "")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=OkRegistry(),
                                 skills={"install_deps": types.SimpleNamespace()}))
    out = nodes["provision"](_state(c, tid, ses, ws, "php"))
    assert out["verdict"] == "stop" and "PHP 平台要求不满足" in out["stop_reason"]
    assert out["failure_kind"] == "env"
    # 闸返回体喂给 report（图内 stop 路由的下一跳）→ 判决卡数据源拿到 env
    rep = nodes["report"]({**_state(c, tid, ses, ws, "php"), **out})
    assert rep is not None
    row = c.execute("SELECT defects FROM test_reports WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert json.loads(row["defects"])["failure_kind"] == "env"
    t = c.execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert t["status"] == "failed" and "PHP 平台要求不满足" in t["error"]


def test_provision_go_toolchain_gate_stop_sets_env_kind(env, tmp_path):
    c, tid, ses = env
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "go.mod").write_text("module demo\n\ngo 1.26\n", encoding="utf-8")
    runner = GateRunner([
        (lambda cmd, j: cmd[:2] == ["go", "version"], lambda: _R(0, "go version go1.22.0 linux/amd64", "")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=OkRegistry(),
                                 skills={"install_deps": types.SimpleNamespace()}))
    out = nodes["provision"](_state(c, tid, ses, ws, "go", image="mta-test:go126"))
    assert out["verdict"] == "stop" and "Go 工具链版本不足" in out["stop_reason"]
    assert out["failure_kind"] == "env"


def test_provision_image_not_ready_stop_sets_env_kind(env, tmp_path):
    c, tid, ses = env
    runner = GateRunner([], image_ok=False)
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=OkRegistry(),
                                 skills={"install_deps": types.SimpleNamespace()}))
    out = nodes["provision"](_state(c, tid, ses, tmp_path, "python",
                                    image="mta-test:missing"))
    assert out["verdict"] == "stop" and "执行镜像未就绪" in out["stop_reason"]
    assert out["failure_kind"] == "env"


def test_run_canary_env_gate_stop_sets_env_kind(env, tmp_path, monkeypatch):
    """金丝雀环境闸（归因三闸之一，旗标默认关）拦停同样带 env kind。"""
    c, tid, ses = env
    monkeypatch.setattr(config, "ENV_GATE_ENABLED", True)
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = GateRunner([
        (lambda cmd, j: "CANARY_OK" in j, lambda: _R(1, "", "pytest: not found")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=OkRegistry(),
                                 skills={"run_tests": types.SimpleNamespace()}))
    out = nodes["run"](_state(c, tid, ses, ws, "python", image="mta-test:py312"))
    assert out["verdict"] == "stop" and "金丝雀" in out["stop_reason"]
    assert out["failure_kind"] == "env"
    # fail-fast 实锤：金丝雀失败后不应有任何真实用例执行命令
    assert not any("pytest" in " ".join(cmd) and "CANARY_OK" not in " ".join(cmd)
                   for cmd in runner.calls)


def test_provision_php_gate_pass_no_kind_leak(env, tmp_path):
    """闸放行（check-platform-reqs exit=0）→ 正常路径不带 failure_kind（非闸路径不变）。"""
    c, tid, ses = env
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "composer.json").write_text('{"require": {"php": "^8.1"}}', encoding="utf-8")
    runner = GateRunner([
        (lambda cmd, j: "check-platform-reqs" in j, lambda: _R(0, "php-64bit 8.3.36 passed", "")),
    ])
    nodes = make_nodes(GraphDeps(conn=c, runner=runner, registry=OkRegistry(),
                                 skills={"install_deps": types.SimpleNamespace()}))
    out = nodes["provision"](_state(c, tid, ses, ws, "php"))
    assert "failure_kind" not in out and out.get("verdict") != "stop"
