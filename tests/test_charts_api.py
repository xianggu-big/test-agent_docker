"""报告页图内嵌：/api/tasks/{tid}/charts 端点（gen_charts 产物 → base64 data URL）。

鉴权与任务存在性走真实链路（TestClient 触发 startup 建临时库+默认账号），
图文件用预写 PNG 字节模拟（gen_charts 的渲染本身由 test_eval_tools 覆盖）。
"""
import base64

import pytest
from fastapi.testclient import TestClient

from test_agent import config


PNG = b"\x89PNG\r\n\x1a\n" + b"fake-chart-bytes" * 8


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
            "INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
            (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
            "VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid
        c.commit()
        c.close()
        yield client, h, tid, tmp_path / "reports" / "charts"


def test_charts_requires_auth(env):
    client, _, tid, _ = env
    assert client.get(f"/api/tasks/{tid}/charts").status_code == 401


def test_charts_empty_when_task_ran_no_evaluation(env):
    """任务没跑评估阶段（无图文件）→ 空数组，报告页显示"暂无图表"。"""
    client, h, tid, _ = env
    r = client.get(f"/api/tasks/{tid}/charts", headers=h)
    assert r.status_code == 200 and r.json() == []


def test_charts_returns_base64_and_titles(env):
    """本任务的图 → base64 data URL + 中文标题；其他任务的图不串进本任务。"""
    client, h, tid, charts_dir = env
    charts_dir.mkdir(parents=True)
    (charts_dir / f"task_{tid}_rtm.png").write_bytes(PNG)
    (charts_dir / f"task_{tid}_coverage_trend.png").write_bytes(PNG)
    (charts_dir / "task_999_rtm.png").write_bytes(PNG)      # 别的任务的图
    r = client.get(f"/api/tasks/{tid}/charts", headers=h)
    assert r.status_code == 200
    data = r.json()
    by_name = {x["name"]: x for x in data}
    assert set(by_name) == {f"task_{tid}_rtm.png", f"task_{tid}_coverage_trend.png"}
    assert by_name[f"task_{tid}_rtm.png"]["title"] == "RTM 需求覆盖"
    assert by_name[f"task_{tid}_coverage_trend.png"]["title"].startswith("覆盖率趋势")
    for x in data:
        assert x["data_url"].startswith("data:image/png;base64,")
        payload = x["data_url"].split(",", 1)[1]
        assert base64.b64decode(payload) == PNG


def test_charts_404_for_missing_task(env):
    client, h, _, _ = env
    assert client.get("/api/tasks/9999/charts", headers=h).status_code == 404
