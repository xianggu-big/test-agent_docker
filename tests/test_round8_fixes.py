"""第八轮修复锁（2026-11-15 闲时找 bug 第八轮，R7 失败可恢复性维度——对准
卡 0-2/L1 服务编排新改动面）。

实锤一处（B2 经取证为第七轮已修的既有行为，转为回归锁）：
1. 服务重启恢复后 queued 任务永久滞留：queue._recover 把遗留 running 队列行标
   failed，但对应任务状态若停在 queued（已放行排队中）——queued 不在 ACTIVE
   清扫集（_sweep_orphan_active_tasks 只扫 ACTIVE），重启后无人收尸；
2. api_stop 取消 queued 任务不取消在途队列行——取证后确认第七轮已修
   （api_stop 已有'排队行同步取消'逻辑，exec_queue 枚举含 cancelled），
   本文件保留该行为的回归锁。
"""
import pytest

from test_agent import config
from test_agent.store.db import get_conn, init_db
from test_agent.store.queue import QueueWorker


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn()
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.commit()
    conn.close()
    return db


def _mk_task(status="queued", two_stage=1):
    conn = get_conn()
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,status,confirmed,two_stage)"
                       " VALUES(?,1,?,1,?)", (pid, status, two_stage)).lastrowid
    conn.commit()
    conn.close()
    return tid


# ---------- 1. 重启恢复：queued 任务连带收尸 ----------

def test_recover_marks_queued_task_failed(tmp_db):
    """running 队列行标 failed 时，停在 queued 的任务必须一并标 failed（旧=永久滞留）。"""
    tid = _mk_task("queued")
    conn = get_conn()
    qid = conn.execute("INSERT INTO exec_queue(task_id,kind,status) VALUES(?,'exec','running')",
                       (tid,)).lastrowid
    conn.commit()
    conn.close()
    QueueWorker()._recover()
    row = get_conn().execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "failed", "重启后 queued 任务必须收尸（旧形态永久滞留）"
    assert "重启" in row["error"]


def test_recover_leaves_terminal_tasks_alone(tmp_db):
    """队列行 running 但任务已 failed（图自己写的精确错误）——不得覆盖。"""
    tid = _mk_task("failed")
    conn = get_conn()
    conn.execute("UPDATE test_tasks SET error='图内精确错误' WHERE id=?", (tid,))
    conn.execute("INSERT INTO exec_queue(task_id,kind,status) VALUES(?,'exec','running')", (tid,))
    conn.commit()
    conn.close()
    QueueWorker()._recover()
    row = get_conn().execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "failed" and row["error"] == "图内精确错误"


def test_recover_leaves_ready_for_review_alone(tmp_db):
    """ready_for_review 是合法停等态（prep 已完成等审查）——不收尸。"""
    tid = _mk_task("ready_for_review")
    conn = get_conn()
    conn.execute("INSERT INTO exec_queue(task_id,kind,status) VALUES(?,'prep','running')", (tid,))
    conn.commit()
    conn.close()
    QueueWorker()._recover()
    row = get_conn().execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "ready_for_review"


# ---------- 2. api_stop：取消 queued 任务连带取消队列行（既有行为回归锁） ----------

def test_stop_queued_cancels_pending_queue_row(tmp_db, tmp_path, monkeypatch):
    """stop 取消 queued 任务连带取消 exec_queue 在途行（cancelled）——既有设计
    （第七轮已修），本测试升级为行为锁防回归：worker 不复核任务状态，队列行
    不清=已取消任务被"复活"全链执行。"""
    from test_agent.api import server as srv
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "d")
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    # 全量回归实锤：前面某测试启动的真 worker 线程（daemon 单例）常驻消费队列，
    # 会把本测试刚插入的 queued 行抢走——patch _step 让常驻线程在本测试内不消费
    monkeypatch.setattr(QueueWorker, "_step", lambda self: False)
    tid = _mk_task("queued")
    conn = get_conn()
    conn.execute("UPDATE test_tasks SET confirmed=1 WHERE id=?", (tid,))
    qid = conn.execute("INSERT INTO exec_queue(task_id,kind,status) VALUES(?,'exec','queued')",
                       (tid,)).lastrowid
    conn.commit()
    conn.close()
    with TestClient(srv.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        resp = client.post(f"/api/tasks/{tid}/stop", headers=h)
        assert resp.status_code == 200
    row = get_conn().execute("SELECT status, error FROM exec_queue WHERE id=?", (qid,)).fetchone()
    assert row["status"] == "cancelled", "取消任务后队列行必须连带取消（防复活执行）"
    assert "取消" in (row["error"] or "")
    trow = get_conn().execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert trow["status"] == "cancelled"
