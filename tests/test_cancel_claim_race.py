"""A4 取消竞态测试（2026-11-20 设计方案 docs/设计方案-文件级修复与验收设计-20261120.md 包 A）。

复现 Qoder 体检论断 5：worker 认领（队列行→running）与 api_stop（任务 queued→cancelled
+队列行清理 WHERE status='queued'）两套账不同事务——认领后任务仍 queued 的窗口内，
stop 返回成功却清不到已 running 的队列行，worker 继续执行（run_task_prep/run_task 的
乐观锁排除表不拦 cancelled）→ 已取消任务"复活"全链执行。

修复契约（三道闸，同一套事务规则下判定谁先成功）：
①认领队列行条件更新（AND status='queued'）——stop 先取消则认领失败；
②认领后任务状态守卫——任务已 cancelled 则队列行如实回写 cancelled 跳过执行；
③执行入口乐观锁排除 cancelled + 显式前置拒绝——最后防线，cancelled 永不进执行链。
铁律：tmp 库、零真实执行、零 Docker。"""
import types

import pytest

from test_agent import config


def _make_task(tmp_path, monkeypatch, *, status="queued", two_stage=1):
    """tmp 库 + 任务 + exec_queue 行，返回 (conn, task_id)。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                       "VALUES('u','x','developer')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "two_stage) VALUES(?,?,0.7,?,1,?)", (pid, uid, status, two_stage)).lastrowid
    conn.commit()
    return conn, tid


def _enqueue(conn, tid, kind="prep", status="queued"):
    qid = conn.execute("INSERT INTO exec_queue(task_id, kind, status) VALUES (?,?,?)",
                       (tid, kind, status)).lastrowid
    conn.commit()
    return qid


def _patch_execs(monkeypatch, calls):
    """patch 三类执行入口与钩子（函数内 import→patch builder/queue 模块属性）。"""
    from test_agent.graph import builder
    from test_agent.store import queue as qmod
    monkeypatch.setattr(builder, "run_task_prep", lambda t: calls.append(("prep", t)))
    monkeypatch.setattr(builder, "run_task_exec", lambda t: calls.append(("exec", t)))
    monkeypatch.setattr(builder, "run_task", lambda t: calls.append(("task", t)))
    monkeypatch.setattr(builder, "post_prep_review_hook", lambda t: None)
    monkeypatch.setattr(qmod.QueueWorker, "_hybrid_review_after_prep", lambda self, t: None)


def _queue_row(conn, qid):
    return conn.execute("SELECT status, error FROM exec_queue WHERE id=?", (qid,)).fetchone()


# ---------- 守卫②：认领守卫拦 cancelled ----------

def test_claim_skips_when_task_cancelled(tmp_path, monkeypatch):
    """stop 已把任务置 cancelled（其队列行清理未命中的防御态）→ 认领必须跳过执行，
    队列行如实标 cancelled（修复前：worker 无视任务状态直接 run_task_prep=复活）。"""
    conn, tid = _make_task(tmp_path, monkeypatch, status="cancelled")
    qid = _enqueue(conn, tid, kind="prep")          # 队列行仍 queued（stop 清理未命中）
    calls = []
    _patch_execs(monkeypatch, calls)
    from test_agent.store.queue import QueueWorker
    QueueWorker()._step()
    assert calls == [], "已取消任务禁止被认领执行（复活拦截）"
    row = _queue_row(conn, qid)
    assert row["status"] == "cancelled", "队列行必须如实标 cancelled（不冒充 done/failed）"


# ---------- 守卫③：执行入口锁拦 cancelled ----------

def test_prep_entry_rejects_cancelled(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch, status="cancelled")
    from test_agent.graph import builder
    with pytest.raises(RuntimeError, match="已取消"):
        builder.run_task_prep(tid)


def test_run_task_entry_rejects_cancelled(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch, status="cancelled", two_stage=0)
    from test_agent.graph import builder
    with pytest.raises(RuntimeError, match="已取消"):
        builder.run_task(tid)


# ---------- stop 端语义（回归锁） ----------

def test_stop_marks_both_rows(tmp_path, monkeypatch):
    """排队中取消：任务→cancelled 且队列行→cancelled，返回 stopped:true（现有行为）。"""
    conn, tid = _make_task(tmp_path, monkeypatch, status="queued")
    qid = _enqueue(conn, tid)
    from test_agent.api.server import api_stop
    ret = api_stop(tid, user={"role": "admin", "username": "t"})
    assert ret["stopped"] is True
    assert conn.execute("SELECT status FROM test_tasks WHERE id=?",
                        (tid,)).fetchone()["status"] == "cancelled"
    assert _queue_row(conn, qid)["status"] == "cancelled"


def test_stop_nonqueued_honest_false(tmp_path, monkeypatch):
    """非排队态零状态变化，如实报 stopped:false（不谎报成功）。"""
    conn, tid = _make_task(tmp_path, monkeypatch, status="ready_for_review")
    from test_agent.api.server import api_stop
    ret = api_stop(tid, user={"role": "admin", "username": "t"})
    assert ret["stopped"] is False
    assert conn.execute("SELECT status FROM test_tasks WHERE id=?",
                        (tid,)).fetchone()["status"] == "ready_for_review"


# ---------- 认领正常路径（回归锁） ----------

def test_claim_normal_prep_flow(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch, status="queued")
    qid = _enqueue(conn, tid, kind="prep")
    calls = []
    _patch_execs(monkeypatch, calls)
    from test_agent.store.queue import QueueWorker
    QueueWorker()._step()
    assert calls == [("prep", tid)], "正常认领→阶段一执行一次"
    assert _queue_row(conn, qid)["status"] == "done"


def test_claim_exec_kind_not_blocked(tmp_path, monkeypatch):
    """exec 类任务（ready_for_review 态）认领不误伤——守卫只拦 cancelled。"""
    conn, tid = _make_task(tmp_path, monkeypatch, status="ready_for_review")
    qid = _enqueue(conn, tid, kind="exec")
    calls = []
    _patch_execs(monkeypatch, calls)
    from test_agent.store.queue import QueueWorker
    QueueWorker()._step()
    assert calls == [("exec", tid)], "exec 类正常认领执行"
    assert _queue_row(conn, qid)["status"] == "done"
