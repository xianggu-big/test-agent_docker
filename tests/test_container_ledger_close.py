"""容器台账收口测试锁（2026-11-16 任务列表倒推实锤：1001 条滞留 running）。

根因：report 节点只走 happy path 收口（job_id 键），图异常/队列失败/重启清扫
路径的 provision 台账行无人收口 → 永久滞留 running（健康度倒推失真）。
修=close_container_jobs helper + 四收口点（builder except/prep 降级经 _fail_task、
queue._fail_task_if_pending、两个 sweep）。
"""
import pytest

from test_agent import config
from test_agent.graph.builder import close_container_jobs
from test_agent.store.db import get_conn, init_db
from test_agent.store.queue import _fail_task_if_pending


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    init_db()
    conn = get_conn()
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.commit()
    yield conn
    conn.close()


def _mk_running_job(conn, tid):
    return conn.execute(
        "INSERT INTO container_jobs(task_id, image, status) VALUES(?, 'mta-test:py312', 'running')",
        (tid,)).lastrowid


def test_close_container_jobs_idempotent(tmp_db):
    pid = tmp_db.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = tmp_db.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,1)",
                         (pid,)).lastrowid
    jid = _mk_running_job(tmp_db, tid)
    tmp_db.commit()
    assert close_container_jobs(tmp_db, tid, "failed") == 1
    row = tmp_db.execute("SELECT status FROM container_jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed"
    assert close_container_jobs(tmp_db, tid, "failed") == 0, "幂等：二次收口零行"


def test_close_only_running_rows(tmp_db):
    """已 done 的行不得被改写（历史真实记录保护）。"""
    pid = tmp_db.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = tmp_db.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,1)",
                         (pid,)).lastrowid
    jid = tmp_db.execute(
        "INSERT INTO container_jobs(task_id, image, status) VALUES(?, 'img', 'done')",
        (tid,)).lastrowid
    tmp_db.commit()
    assert close_container_jobs(tmp_db, tid, "failed") == 0
    assert tmp_db.execute("SELECT status FROM container_jobs WHERE id=?",
                          (jid,)).fetchone()["status"] == "done"


def test_queue_failure_closes_ledger(tmp_db, monkeypatch):
    """queue._fail_task_if_pending（状态机四断点之二）连带收口台账。"""
    pid = tmp_db.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = tmp_db.execute("INSERT INTO test_tasks(project_id,user_id,status) VALUES(?,1,'queued')",
                         (pid,)).lastrowid
    _mk_running_job(tmp_db, tid)
    tmp_db.commit()
    _fail_task_if_pending(tid, "boom")
    row = tmp_db.execute("SELECT status FROM container_jobs WHERE task_id=?", (tid,)).fetchone()
    assert row["status"] == "failed", "队列失败回写必须连带收口容器台账"


def test_sweep_stale_closes_ledger(tmp_db, monkeypatch):
    """陈旧 ACTIVE sweep 把任务标 failed 时连带收口台账行。"""
    from test_agent.api.server import _sweep_stale_active_tasks
    pid = tmp_db.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = tmp_db.execute(
        "INSERT INTO test_tasks(project_id,user_id,status,updated_at)"
        " VALUES(?,1,'running',datetime('now','-3 hours'))", (pid,)).lastrowid
    _mk_running_job(tmp_db, tid)
    tmp_db.commit()
    _sweep_stale_active_tasks(max_age_s=2 * 3600)
    row = tmp_db.execute("SELECT status FROM container_jobs WHERE task_id=?", (tid,)).fetchone()
    assert row["status"] == "failed", "sweep 收尸必须连带收口容器台账"
