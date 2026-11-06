"""黑包阶段1·应用日志端点：GET /api/tasks/{tid}/app_logs?round=N。

路径偏差入档（终验 2026-11-07）：任务卡写 /logs，但该路径自 81d5d86 起被既有
api_logs（agent_messages 日志流，两阶段设计文档口径）占用——同路径双 handler
会被 FastAPI 按注册顺序遮蔽（终验实锤）；本端点改 /app_logs（与表名一致）让位。

鉴权与任务存在性走真实链路（TestClient 触发 startup 建临时库+默认账号），
app_logs 行用 store_app_logs 构造（解析/幂等语义由 test_app_logs 覆盖）。
"""
import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.api import server
    with TestClient(server.app) as client:      # with 触发 startup：init_db + 默认账号
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute(
            "INSERT INTO projects(user_id,repo_url,name,primary_language) "
            "VALUES(?,'r','demo','python')", (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,status,confirmed) "
            "VALUES(?,?,'failed',1)", (pid, uid)).lastrowid
        c.commit()
        c.close()
        yield client, h, tid


def _seed_two_rounds(tid: int) -> None:
    from test_agent.store import db
    from test_agent.skills.app_logs import store_app_logs
    c = db.get_conn()
    r1 = [{"line_no": 3, "level": "error", "kind": "python_traceback",
           "message": "Traceback (most recent call last):", "raw_line": "Traceback (most recent call last):"},
          {"line_no": 7, "level": "warn", "kind": "warn_log",
           "message": "WARNING:  retry", "raw_line": "WARNING:  retry"}]
    r2 = [{"line_no": 2, "level": "error", "kind": "python_exception",
           "message": "RuntimeError: boom", "raw_line": "RuntimeError: boom"}]
    store_app_logs(c, tid, 1, r1, source="file",
                   log_path="data/workspaces/task_x/logs/app_r1.log", log_lines=7)
    store_app_logs(c, tid, 2, r2, source="docker_logs",
                   log_path="data/workspaces/task_x/logs/app_r2.log", log_lines=5)
    c.close()


def test_logs_requires_auth(env):
    client, _, tid = env
    assert client.get(f"/api/tasks/{tid}/app_logs").status_code == 401


def test_logs_404_unknown_task(env):
    client, h, _ = env
    assert client.get("/api/tasks/99999/app_logs", headers=h).status_code == 404


def test_logs_returns_all_rounds_then_filters(env):
    client, h, tid = env
    _seed_two_rounds(tid)
    r = client.get(f"/api/tasks/{tid}/app_logs", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid
    rounds = {x["round_no"]: x for x in body["rounds"]}
    assert set(rounds) == {1, 2}
    assert rounds[1]["error_count"] == 1 and rounds[1]["warn_count"] == 1
    assert rounds[1]["first_error_line"] == 3
    assert rounds[1]["source"] == "file"
    assert len(rounds[1]["entries"]) == 2
    assert rounds[2]["source"] == "docker_logs"
    # round 过滤：单轮
    r2 = client.get(f"/api/tasks/{tid}/app_logs?round=2", headers=h)
    assert r2.status_code == 200
    only = r2.json()["rounds"]
    assert [x["round_no"] for x in only] == [2]
    assert only[0]["entries"][0]["kind"] == "python_exception"


def test_logs_empty_task_returns_empty_rounds(env):
    client, h, tid = env
    r = client.get(f"/api/tasks/{tid}/app_logs", headers=h)
    assert r.status_code == 200
    assert r.json()["rounds"] == []
