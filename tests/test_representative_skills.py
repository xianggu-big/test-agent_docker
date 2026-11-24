"""T6 代表 Skill 四类测试矩阵（学院达标，2026-11-25）。

代表 Skill：主=run_tests（内置·high·显式确认门），辅=test.observe.read（Agent 能力型）。
四类矩阵（Skill 辅方向条款 (5)：正常/异常/权限/边界），每类 ≥1 条真实测试；
与既有覆盖的映射表见 docs/代表Skill契约-run_tests与test.observe.read-20261125.md。

命名约定：test_matrix_<类>_*（验收用 pytest -k test_matrix_ 逐类跑绿）。
"""
import json
import types

import pytest

from test_agent import config
from test_agent.store import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid_ok = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,"
                       "status,confirmed) VALUES(?,?,0.7,'running',1)",
                       (pid, uid)).lastrowid
    tid_lock = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,"
                         "status,confirmed) VALUES(?,?,0.7,'running',0)",
                         (pid, uid)).lastrowid
    c.commit()
    yield c, tid_ok, tid_lock, tmp_path
    c.close()


def _fake_runner(raises=True):
    def _get(name):
        if raises:
            raise Exception("container not found (test)")
        return types.SimpleNamespace(name=name)
    return types.SimpleNamespace(
        client=types.SimpleNamespace(containers=types.SimpleNamespace(get=_get)))


def _registry(c):
    from test_agent.skills.registry import SkillRegistry
    return SkillRegistry(conn=c)


# ---------- 正常类 ----------

def test_matrix_normal_run_tests_container_ok(env):
    """正常类（run_tests）：容器可达 → handler 返回执行结果 JSON（ok 审计行）。"""
    c, tid_ok, _, _ = env
    from test_agent.skills.builtin import build_skills
    runner = _fake_runner(raises=False)
    runner.exec_in = lambda container, cmd, timeout_s=30, **kw: types.SimpleNamespace(
        exit_code=1, stdout="1 passed", stderr="")
    skills = build_skills(runner)
    r = _registry(c).invoke(tid_ok, skills["run_tests"],
                            {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]})
    assert r.status == "ok"
    payload = json.loads(r.output)
    assert payload["exit"] == 1 and "1 passed" in payload["stdout"]


def test_matrix_normal_observe_read(env):
    """正常类（observe.read）：读文件返回窗口化内容 + 证据指针。"""
    c, tid_ok, _, tmp_path = env
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    res = _invoke_observe(c, tid_ok, tmp_path,
                          {"path": "a.py", "mode": "read"})
    assert res["status"] == "ok"
    assert "a.py" in res["summary"] and "行" in res["summary"]   # 窗口头（内容在证据文件）
    assert res["evidence"] and res["evidence"][0]["kind"] == "file_read"


# ---------- 异常类 ----------

def test_matrix_exception_run_tests_container_missing(env):
    """异常类（run_tests）：容器缺失 → handler 诚实环境降级 JSON（不是裸 404）。"""
    c, tid_ok, _, _ = env
    from test_agent.skills.builtin import build_skills
    skills = build_skills(_fake_runner(raises=True))
    r = _registry(c).invoke(tid_ok, skills["run_tests"],
                            {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]})
    assert r.status == "ok"          # handler 正常返回（异常被结构化）
    payload = json.loads(r.output)
    assert payload["exit"] == -1 and "环境错误" in payload["stderr"]


def test_matrix_exception_observe_read_missing_file(env):
    """异常类（observe.read）：文件不存在 → 诚实失败不虚构内容。"""
    c, tid_ok, _, tmp_path = env
    (tmp_path / "ws").mkdir()
    res = _invoke_observe(c, tid_ok, tmp_path, {"path": "ghost.py", "mode": "read"})
    assert res["status"] == "failed"
    assert "ghost.py" in json.dumps(res, ensure_ascii=False) or res["summary"]


# ---------- 权限类 ----------

def test_matrix_permission_confirmation_gate(env):
    """权限类（run_tests）：confirmed=0 → 确认门 fail-closed 拒绝落审计。"""
    c, _, tid_lock, _ = env
    from test_agent.skills.builtin import build_skills
    skills = build_skills(_fake_runner(raises=False))
    r = _registry(c).invoke(tid_lock, skills["run_tests"],
                            {"container_name": "mta-run-1", "cmd": ["python"]})
    assert r.status == "denied" and "确认门" in r.error


def test_matrix_permission_disabled_skill(env):
    """权限类（observe.read）：enabled=0 → 停用闸拒绝（列存在且被查询的实证）。"""
    c, tid_ok, _, tmp_path = env
    skill = _register_observe(c, tmp_path)
    c.execute("UPDATE skills SET enabled=0 WHERE name=?", (skill.name,))
    c.commit()
    r = _registry(c).invoke(tid_ok, skill, {"path": "a.py", "mode": "read"})
    assert r.status == "denied" and "停用" in r.error


# ---------- 边界类 ----------

def test_matrix_boundary_observe_limit_schema(env):
    """边界类（observe.read）：limit 上限 200（=200 放行 / 201 拒）。"""
    c, tid_ok, _, tmp_path = env
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 201)),
                             encoding="utf-8")
    res_ok = _invoke_observe(c, tid_ok, tmp_path,
                             {"path": "a.py", "mode": "read", "limit": 200})
    assert res_ok["status"] == "ok"
    res_denied = _invoke_observe(c, tid_ok, tmp_path,
                                 {"path": "a.py", "mode": "read", "limit": 201})
    assert res_denied["status"] == "denied" and "200" in res_denied["summary"]


def test_matrix_boundary_registry_timeout(env):
    """边界类（注册表）：超时闸 → status=timeout + cancel_event 通知协议。"""
    c, tid_ok, _, _ = env
    import time as _t
    from test_agent.skills.registry import Skill

    def slow(task_id, args, cancel_event=None):
        for _ in range(20):          # 可被 cancel_event 提前打断（0-C 协议）
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            _t.sleep(0.1)
        return "done"

    skill = Skill(name="slow_skill", description="超时演示", func=slow,
                  risk_level="low", timeout_ms=150)
    r = _registry(c).invoke(tid_ok, skill, {})
    assert r.status == "timeout"
    assert r.invocation_id > 0       # 审计行完整（含超时态）


# ---------- 复用助手 ----------

def _register_observe(c, tmp_path):
    """把 test.observe.read Capability 包装成 Skill 注册（tool_loop._execute_capability
    同款形态），返回注册后的 Skill（registry 内 upsert 后的实例）。"""
    from pathlib import Path
    from test_agent.skills.capabilities import TaskCtx, find_capability_extended
    from test_agent.skills.capabilities import normalize_result
    import dataclasses as _dc
    from test_agent.skills.registry import Skill

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    ctx = TaskCtx(task_id=1, session_id=0, workspace=ws, container_name="-",
                  primary_language="python", image="-", state_snapshot={},
                  conn=c, runner=None, artifacts_dir=tmp_path / "artifacts")
    cap = find_capability_extended(ctx, "test.observe.read")

    def _run(_task_id, _args):
        r = normalize_result(cap.handler(ctx, dict(_args)))
        return json.dumps(_dc.asdict(r), ensure_ascii=False)

    skill = Skill(name=cap.id, description=cap.description, func=_run,
                  risk_level="low", timeout_ms=5000, arg_schema=cap.inputs_schema)
    registry = _registry(c)
    registry.register(skill)
    return skill


def _invoke_observe(c, tid, tmp_path, args: dict) -> dict:
    """经注册表调用观察能力并解析 CapResult JSON（denied 时回退审计行状态/错误）。"""
    skill = _register_observe(c, tmp_path)
    r = _registry(c).invoke(tid, skill, args)
    assert r.invocation_id > 0
    try:
        data = json.loads(r.output or "{}")
    except (TypeError, ValueError):
        data = {}
    if not data:
        data = {"status": r.status, "summary": r.error}
    return data
