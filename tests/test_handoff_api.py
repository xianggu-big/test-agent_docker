"""缺陷交接端点：/api/tasks/{tid}/handoff（确定性导出两份交付物文本）。

鉴权与任务存在性走真实链路（TestClient 触发 startup 建临时库+默认账号），
缺陷证据用最小种子行构造（load_defects 的过滤/合并语义由 test_defect_handoff 覆盖）。
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
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
            "VALUES(?,?,0.7,'failed',1)", (pid, uid)).lastrowid
        sid = c.execute(
            "INSERT INTO test_scripts(task_id,framework,content,content_hash) "
            "VALUES(?,'pytest','pass','h')", (tid,)).lastrowid
        c.execute(
            "INSERT INTO test_cases(script_id,source,name,method,content,priority,"
            "expected,expected_basis) VALUES(?,'ai','test_a','边界值分析','def test_a(): pass',"
            "'高','抛 ZeroDivisionError','需求明示')", (sid,))
        c.execute(
            "INSERT INTO test_case_results(task_id,round_no,level,nodeid,name,status,"
            "message,origin) VALUES(?,1,'L1','t.py::test_a','test_a','failed','boom',"
            "'generated')", (tid,))
        c.commit()
        c.close()
        yield client, h, tid


def test_handoff_requires_auth(env):
    client, _, tid = env
    assert client.get(f"/api/tasks/{tid}/handoff").status_code == 401


def test_handoff_returns_both_deliverables(env):
    """有疑似缺陷 → report 与 prompt 双文本 + 计数，护栏三关键词必须在场。"""
    client, h, tid = env
    r = client.get(f"/api/tasks/{tid}/handoff", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["defects"] == 1 and data["high"] == 1
    assert "缺陷报告单" in data["report"] and "BUG-01" in data["report"]
    assert "禁止修改、删除、跳过下列测试用例与断言" in data["prompt"]
    assert "预期待人工裁决" in data["prompt"] and "验证全绿" in data["prompt"]


def test_handoff_404_when_no_defects(env):
    """全绿任务 → 404 诚实告知，不造文档。"""
    client, h, tid = env
    c = __import__("test_agent.store.db", fromlist=["get_conn"]).get_conn()
    c.execute("DELETE FROM test_case_results WHERE task_id=?", (tid,))
    c.commit()
    c.close()
    r = client.get(f"/api/tasks/{tid}/handoff", headers=h)
    assert r.status_code == 404 and "无疑似产品缺陷" in r.json()["detail"]


def test_handoff_404_for_missing_task(env):
    client, h, _ = env
    assert client.get("/api/tasks/9999/handoff", headers=h).status_code == 404
