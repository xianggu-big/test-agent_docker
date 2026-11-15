"""状态机四断点修复锁（2026-11-15，任务卡-错误清扫与找bug收尾 卡 1）。

放行疑云实锤（1139-1147 用户实测）：①放行后任务停在 ready_for_review——worker
排队期间用户无从分辨点没点上；②worker 失败只写 exec_queue，任务永不落终态
（1144/1145 队列 failed 而任务"仍待审查"）；③worker 被杀后任务永久滞留 ACTIVE
（04:33 悬挂家族）。四修：入队置 queued / 失败终态回写 / 陈旧 sweep / 前端文案。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from test_agent import config
from test_agent.store.db import get_conn, init_db
from test_agent.store.queue import QueueWorker, _fail_task_if_pending


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)   # 防真 worker 干扰
    from test_agent.api import server as srv
    monkeypatch.setattr(srv, "env_gate", lambda: None)             # 测试环境无 Docker
    with TestClient(srv.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        staff_h = {"Authorization": f"Bearer {r.json()['token']}"}
        c = get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,two_stage)"
            " VALUES(?,?,0.7,'ready_for_review',1,1)", (pid, uid)).lastrowid
        c.commit()
        c.close()
        yield client, staff_h, tid, srv


def _task(tid):
    c = get_conn()
    try:
        return c.execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()
    finally:
        c.close()


# ---------- 断点 1：放行即置 queued ----------

def test_release_marks_queued(env):
    client, staff_h, tid, _srv = env
    r = client.post(f"/api/tasks/{tid}/run", headers=staff_h)
    assert r.status_code == 200 and r.json()["queued"]
    assert _task(tid)["status"] == "queued", "放行后必须可见'排队等待执行'（旧=停 ready_for_review）"
    c = get_conn()
    try:
        row = c.execute("SELECT status FROM exec_queue WHERE task_id=? AND kind='exec'",
                        (tid,)).fetchone()
    finally:
        c.close()
    assert row and row["status"] == "queued"


def test_double_release_honest_409(env):
    """已排队再点放行：409 诚实告知排队中（旧=误导性"需先完成阶段一"）。"""
    client, staff_h, tid, _srv = env
    assert client.post(f"/api/tasks/{tid}/run", headers=staff_h).status_code == 200
    r = client.post(f"/api/tasks/{tid}/run", headers=staff_h)
    assert r.status_code == 409 and "队列中等待执行" in r.json()["detail"]


def test_single_stage_release_marks_queued(env):
    """单阶段任务重跑（failed → 重新下发）同样置 queued。"""
    client, staff_h, tid, _srv = env
    c = get_conn()
    c.execute("UPDATE test_tasks SET two_stage=0, status='failed' WHERE id=?", (tid,))
    c.commit()
    c.close()
    r = client.post(f"/api/tasks/{tid}/run", headers=staff_h)
    assert r.status_code == 200
    assert _task(tid)["status"] == "queued"


# ---------- 断点 2：worker 失败终态回写 ----------

def test_worker_failure_writes_back_task(tmp_path, monkeypatch):
    """锁前异常（SQLite busy/守卫前炸）只落队列行——旧形态任务永远停在原态
    （1144/1145 实锤）。worker 失败必须把任务带到 failed 终态。"""
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    c = get_conn()
    c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,status,confirmed,two_stage)"
                    " VALUES(?,1,'queued',1,0)", (pid,)).lastrowid
    c.commit()
    c.close()
    QueueWorker.enqueue(tid, "exec")

    import test_agent.graph.builder as builder

    def boom(task_id):
        raise RuntimeError("锁前爆炸（模拟 SQLite busy/守卫前异常）")

    monkeypatch.setattr(builder, "run_task", boom)
    w = QueueWorker()
    assert w._step() is True
    row = _task(tid)
    assert row["status"] == "failed", "worker 失败后任务不得停留在原态"
    assert "队列执行失败" in row["error"] and "锁前爆炸" in row["error"]


def test_worker_failure_respects_terminal(tmp_path, monkeypatch):
    """图自己 _fail_task 写的精确错误不覆盖（status 已 failed → 回写零行）。"""
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    c = get_conn()
    c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,status,error)"
                    " VALUES(?,1,'failed','APIError: 409 精确错误')", (pid,)).lastrowid
    c.commit()
    c.close()
    _fail_task_if_pending(tid, "队列执行失败：笼统错误")
    row = _task(tid)
    assert row["status"] == "failed" and row["error"] == "APIError: 409 精确错误"


def test_exec_lock_accepts_queued(tmp_path, monkeypatch):
    """放行即置 queued 后，run_task_exec 放行锁必须兼容 queued（否则 worker 取行
    即撞"任务不在待审查状态"，任务永远启动不了）。工作区缺失路径：锁成功→
    provisioning→_fail_task('工作区不存在')，全程无 Docker。"""
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = get_conn()
    c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,status,confirmed,two_stage)"
                    " VALUES(?,1,'queued',1,1)", (pid,)).lastrowid
    c.commit()
    c.close()
    from test_agent.graph.builder import run_task_exec
    with pytest.raises(RuntimeError, match="工作区不存在"):
        run_task_exec(tid)
    row = _task(tid)
    assert row["status"] == "failed" and "工作区不存在" in row["error"], \
        "锁必须接受 queued（报错若是'任务不在待审查状态'即回归）"


# ---------- 断点 3：陈旧中间态 sweep ----------

def test_sweep_stale_active_tasks(tmp_path, monkeypatch):
    from test_agent.api.server import _sweep_stale_active_tasks
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    c = get_conn()
    c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    old_run = c.execute("INSERT INTO test_tasks(project_id,user_id,status,error,updated_at)"
                        " VALUES(?,1,'running',NULL,datetime('now','-3 hours'))",
                        (pid,)).lastrowid
    fresh_run = c.execute("INSERT INTO test_tasks(project_id,user_id,status,updated_at)"
                          " VALUES(?,1,'running',datetime('now'))", (pid,)).lastrowid
    old_rfr = c.execute("INSERT INTO test_tasks(project_id,user_id,status,updated_at)"
                        " VALUES(?,1,'ready_for_review',datetime('now','-3 hours'))",
                        (pid,)).lastrowid
    c.commit()
    c.close()
    n = _sweep_stale_active_tasks(max_age_s=2 * 3600)
    rows = {r["id"]: r["status"] for r in
            (get_conn().execute("SELECT id,status FROM test_tasks").fetchall())}
    assert rows[old_run] == "failed", "滞留 ACTIVE 超时必须被清扫"
    assert rows[fresh_run] == "running", "活跃任务（updated_at 新鲜）不得误杀"
    assert rows[old_rfr] == "ready_for_review", "待审查是合法停等态，不清扫"
    assert n == 1


def test_sweep_tolerates_sqlite_error(tmp_path, monkeypatch):
    """清扫失败（库忙）不炸周期线程——_run_one_sweep 吞异常。"""
    from test_agent.api import server as srv

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(srv, "_sweep_stale_active_tasks", boom)
    srv._run_one_sweep()   # 不抛即过（旧形态直接 raise 会杀死周期线程）
