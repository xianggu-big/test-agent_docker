"""T0-2 路由防复发守卫（2026-11-25 学院达标）。

实锤：`POST /api/tasks/{tid}/verify-restore` 的路由装饰器丢失——2026-11-21 包 3
在 `api_agent_loop`（server.py:802）上方插入端点时吃掉了 `api_verify_restore`
的 `@app.post(...)` 行，该函数成为全文件唯一没有路由装饰器的 `api_*`。前端无
调用方，所以 11-21 记账的"0-A 人工恢复端点"自上线起就不可达，一直没人发现。

三个守卫：
1. 业务 404 而非路由缺失 404（不存在任务 → "任务 N 不存在"）；
2. AST 守卫：server.py 顶层每个 `def api_*` 必须带 `@app.<method>` 装饰器；
3. 非验证任务 → 业务 400（verify_mode=0 无冻结快照可恢复）。
"""
from ast import Attribute, Call, FunctionDef, Name, parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server, auth
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        staff_h = {"Authorization": f"Bearer {r.json()['token']}"}
        yield client, staff_h, tmp_path


def test_verify_restore_route_exists_business_404(env):
    """不存在任务 → 业务 404（"任务 N 不存在"），不是路由缺失的 {"detail":"Not Found"}。"""
    client, staff_h, _ = env
    r = client.post("/api/tasks/999999/verify-restore", headers=staff_h)
    assert r.status_code == 404
    detail = r.json().get("detail", "")
    assert "不存在" in detail, (
        f"路由疑似缺失（detail={detail!r}）——FastAPI 默认 404 是 Not Found，"
        "业务 404 应带任务 id"
    )


def test_server_api_functions_have_route_decorator():
    """AST 守卫：server.py 顶层每个 def api_* 必须紧邻 @app.<method> 装饰器。"""
    from test_agent.api import server
    src = Path(server.__file__).read_text(encoding="utf-8")
    tree = parse(src)
    missing = []
    for node in tree.body:
        if not (isinstance(node, FunctionDef) and node.name.startswith("api_")):
            continue
        has_route = any(
            isinstance(d, Call)
            and isinstance(d.func, Attribute)
            and isinstance(d.func.value, Name)
            and d.func.value.id == "app"
            for d in node.decorator_list
        )
        if not has_route:
            missing.append(node.name)
    assert not missing, f"这些 api_* 函数缺少 @app.<method> 路由装饰器: {missing}"


def test_verify_restore_non_verify_task_400(env):
    """verify_mode=0 的任务 → 400（不是验证任务，无冻结快照可恢复）。"""
    client, staff_h, _ = env
    from test_agent.store import db
    c = db.get_conn()
    uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
        "VALUES(?,?,0.7,'failed',1)", (pid, uid)).lastrowid
    c.commit()
    c.close()
    r = client.post(f"/api/tasks/{tid}/verify-restore", headers=staff_h)
    assert r.status_code == 400
    assert "不是验证任务" in r.json()["detail"]
