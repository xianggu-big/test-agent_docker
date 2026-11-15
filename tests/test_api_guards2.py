"""API 守卫补漏（2026-11-15 闲时找bug 第一轮，R6 安全面维度）。

两个实锤：
1. POST /api/tasks/{tid}/verify-fix 挂 current_user——唯一一个 caller 可触发的
   变更类写端点（创建派生任务、复制用例集、直置 ready_for_review+confirmed=1）。
   同文件权限表注释明确 caller 仅"新建·列表·报告"；其余变更端点（confirm/run/
   stop/cases/feedback/spotcheck）全挂 staff。对齐 staff。
2. POST /api/tasks/{tid}/l4/convert 写文件前不查工作区——从未跑过/工作区被清的
   任务点转化 → 裸 FileNotFoundError 500，而兄弟端点 api_add_case 是诚实 409。
"""
import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)   # 防真 worker 干扰
    from test_agent.api import server, auth
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        staff_h = {"Authorization": f"Bearer {r.json()['token']}"}
        auth.register("g2_caller", "caller123")
        r2 = client.post("/api/login", json={"username": "g2_caller", "password": "caller123"})
        caller_h = {"Authorization": f"Bearer {r2.json()['token']}"}
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
        yield client, staff_h, caller_h, tid, tmp_path


def test_verify_fix_requires_staff(env):
    """caller 不得创建验证任务（变更类端点=staff 专属，权限表口径）。"""
    client, _staff_h, caller_h, tid, _ = env
    r = client.post(f"/api/tasks/{tid}/verify-fix", headers=caller_h)
    assert r.status_code == 403, "verify-fix 是变更类端点，caller 必须被拒"


def test_l4_convert_missing_workspace_honest_409(env):
    """工作区不存在（任务从未跑过/被清）→ 409 诚实提示，不是裸 500。"""
    client, staff_h, _caller_h, tid, tmp_path = env
    from test_agent.store import db
    c = db.get_conn()
    c.execute("INSERT INTO scenario_cases(task_id,level,title,steps,status) "
              "VALUES(?, 'L4', '验收场景', '[]', 'confirmed_pass')", (tid,))
    c.commit()
    c.close()
    assert not (tmp_path / "workspaces" / f"task_{tid}").exists()
    r = client.post(f"/api/tasks/{tid}/l4/convert", headers=staff_h)
    assert r.status_code == 409, "工作区缺失应 409（api_add_case 同款），不是 500"
