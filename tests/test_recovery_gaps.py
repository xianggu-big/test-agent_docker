"""失败可恢复性缺口修复锁（2026-11-15 闲时找bug 第一轮，R3/R7 维度）。

三个实锤缺口：
1. levels L1 leg 置 running 后 run_task 抛异常直接上抛 → test_levels.L1 永远
   "运行中"（四级视图假运行态）；
2. api_stop 取消"排队中"任务只改 test_tasks 不动 exec_queue → worker 照样取到
   队列行把已取消任务"复活"全链执行（builder 乐观锁不拦 cancelled）；
3. 服务重启孤儿容器清扫只认 mta-run- 前缀 → ui_idempotency 的 mta-ui-* 容器
   （含 chromium 常驻型）无限累积。
"""
import pytest
from fastapi.testclient import TestClient

from test_agent import config


# ---------- 1. levels L1 leg 异常收口 ----------

def test_l1_leg_marked_failed_when_run_task_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
        "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    c.commit()
    c.close()

    from test_agent.graph import levels as lv

    def boom(task_id):
        raise RuntimeError("克隆失败（模拟图异常上抛）")

    monkeypatch.setattr(lv, "run_task", boom)
    with pytest.raises(RuntimeError):
        lv.run_task_levels(tid, levels=("L1",))
    c = db.get_conn()
    row = c.execute("SELECT status FROM test_levels WHERE task_id=? AND level='L1'",
                    (tid,)).fetchone()
    c.close()
    assert row is not None, "L1 leg 行应已建立"
    assert row["status"] == "failed", "图异常上抛前 leg 必须收口 failed（旧=永远 running）"


# ---------- 2. api_stop 清排队行 ----------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    # 隔离真 worker：startup 会拉起队列线程，1.5s 轮询可能抢走本测试 seed 的 queued 行
    # （置 running）→ api_stop 只清 queued → 偶发竞态红（全量跑第 3 次实锤）
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
            "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
        qid = c.execute("INSERT INTO exec_queue(task_id, kind) VALUES(?, 'exec')",
                        (tid,)).lastrowid
        c.commit()
        c.close()
        yield client, h, tid, qid


def test_stop_queued_task_cancels_queue_row(env):
    """排队中任务点停止：exec_queue 行必须同步取消，否则 worker 取到后任务复活。"""
    client, h, tid, qid = env
    r = client.post(f"/api/tasks/{tid}/stop", headers=h)
    assert r.status_code == 200 and r.json()["stopped"] is True
    from test_agent.store import db
    c = db.get_conn()
    q = c.execute("SELECT status FROM exec_queue WHERE id=?", (qid,)).fetchone()
    t = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    assert t["status"] == "cancelled"
    assert q["status"] == "cancelled", "队列行残留 queued=已取消任务会被 worker 复活"


def test_stop_untouched_when_no_queue_row(env):
    """无队列行的 queued 任务停止：原语义不变（不炸、任务 cancelled）。"""
    client, h, tid, _qid = env
    from test_agent.store import db
    c = db.get_conn()
    c.execute("DELETE FROM exec_queue")
    c.commit()
    c.close()
    r = client.post(f"/api/tasks/{tid}/stop", headers=h)
    assert r.status_code == 200
    c = db.get_conn()
    t = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    assert t["status"] == "cancelled"


# ---------- 3. 孤儿容器清扫覆盖 mta-ui- ----------

def test_sweep_orphan_containers_covers_ui_prefix(monkeypatch):
    from test_agent.api import server

    seen_filters = []
    removed = []

    class _C:
        def __init__(self, name):
            self.name = name

        def remove(self, force=False):
            removed.append(self.name)

    class _FakeContainers:
        def list(self, filters=None):
            name = filters and filters.get("name")
            seen_filters.append(name)
            return [_C("mta-run-9")] if name == "mta-run-" else [_C("mta-ui-9")]

    class _FakeClient:
        containers = _FakeContainers()

    class _FakeRunner:
        client = _FakeClient()

    monkeypatch.setattr(server, "get_runner", lambda: _FakeRunner())
    server._sweep_orphan_containers()
    assert set(seen_filters) == {"mta-run-", "mta-ui-"}, "清扫必须同时覆盖两种前缀"
    assert removed == ["mta-run-9", "mta-ui-9"]
