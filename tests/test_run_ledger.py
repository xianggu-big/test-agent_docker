"""T2 运行账本回归：agent_runs / agent_steps 两表 + start/finish/record/reconcile 纯函数。

设计稿 §6（docs/学院达标-任务规划与设计-20261124.md）：账本是旁路——只多写两张表，
不改提示词、不改图、不改任何现有输出。开关 RUN_LEDGER_ENABLED 默认关（Q1 未拍板按默认）。
"""
import sqlite3

import pytest

from test_agent import config
from test_agent.store import db


@pytest.fixture()
def conn(tmp_path):
    db.init_db(tmp_path / "t.db")
    c = db.get_conn(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute(
        "INSERT INTO test_tasks(project_id, user_id, coverage_target, status, confirmed) "
        "VALUES (?,?,0.7,'running',1)", (pid, uid)).lastrowid
    c.commit()
    yield c, tid
    c.close()


def test_init_db_returns_39(tmp_path):
    """守卫同步：37 → 39（agent_runs / agent_steps，学院达标 T2）。"""
    assert db.init_db(tmp_path / "t.db") == 39


def test_taskstate_declares_run_keys():
    """TaskState 必须显式声明 run_id / run_started_ms，否则被 LangGraph 静默过滤
    （366 / two_stage_rebuild 两次实锤的老坑）。"""
    from test_agent.graph.state import TaskState
    assert "run_id" in TaskState.__annotations__
    assert "run_started_ms" in TaskState.__annotations__


def test_run_ledger_flag_defaults_off():
    """Q1 未拍板按默认关：关 = 两表零写入，其余行为逐字节不变。"""
    assert config.RUN_LEDGER_ENABLED is False


def test_start_run_creates_session_and_run(conn):
    """start_run 一口建两行（agent_sessions + agent_runs），attempt_no 首次=1。"""
    from test_agent.skills.run_ledger import start_run
    c, tid = conn
    sid, rid = start_run(c, tid, kind="single", goal_text="检出 clamp 缺陷")
    row = c.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row["session_id"] == sid
    assert row["task_id"] == tid
    assert row["attempt_no"] == 1
    assert row["kind"] == "single"
    assert row["status"] == "running"
    assert row["started_ms"] is not None and row["started_ms"] > 0
    sess = c.execute("SELECT * FROM agent_sessions WHERE id=?", (sid,)).fetchone()
    assert sess["task_id"] == tid


def test_attempt_no_rules(conn):
    """single/prep/d1_review=新尝试（max+1）；exec=归入最新尝试。"""
    from test_agent.skills.run_ledger import start_run
    c, tid = conn
    _, r1 = start_run(c, tid, kind="single")
    _, r2 = start_run(c, tid, kind="single")
    assert c.execute("SELECT attempt_no FROM agent_runs WHERE id=?", (r2,)).fetchone()[0] == 2
    _, r3 = start_run(c, tid, kind="exec")
    assert c.execute("SELECT attempt_no FROM agent_runs WHERE id=?", (r3,)).fetchone()[0] == 2
    _, r4 = start_run(c, tid, kind="prep")
    assert c.execute("SELECT attempt_no FROM agent_runs WHERE id=?", (r4,)).fetchone()[0] == 3


def test_finish_run_sets_ended_and_session(conn):
    """finish_run 收口 run 行（ended_ms/status/stop_reason/summary）+ 同步会话 ended_at。"""
    from test_agent.skills.run_ledger import finish_run, start_run
    c, tid = conn
    sid, rid = start_run(c, tid, kind="single")
    finish_run(c, rid, status="failed", stop_reason="疑似产品缺陷：断言失败用例保留为证人",
               summary_json='{"rounds":1}')
    row = c.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "failed"
    assert row["ended_ms"] is not None and row["ended_ms"] >= row["started_ms"]
    assert "疑似产品缺陷" in row["stop_reason"]
    assert c.execute("SELECT ended_at FROM agent_sessions WHERE id=?",
                     (sid,)).fetchone()[0] is not None


def test_record_step_roundtrip_and_cap(conn):
    """步骤行全字段往返；decision_json 封顶 8000 字符；外键可 JOIN。"""
    from test_agent.skills.run_ledger import record_step, start_run
    c, tid = conn
    sid, rid = start_run(c, tid, kind="single")
    big = "x" * 9000
    step_id = record_step(
        c, rid, mode="strategy", loop_no=1, step_no=1, action_type="call",
        capability="test.observe.read", args={"path": "src/calc.js"},
        decision_json=big, allowed=1, llm_call_id=None, invocation_id=None,
        result_status="ok", result_summary="读取 120 行",
        evidence=[{"kind": "file", "path": "src/calc.js", "sha256": "abc"}],
        decision_ms=1200, exec_ms=340)
    row = c.execute("SELECT * FROM agent_steps WHERE id=?", (step_id,)).fetchone()
    assert len(row["decision_json"]) == 8000, "decision_json 封顶 8000 字符"
    assert row["allowed"] == 1
    assert row["result_status"] == "ok"
    assert "calc.js" in row["args_json"]
    assert "calc.js" in row["evidence_json"]
    # denied 步：deny_reason 落原文
    record_step(c, rid, mode="strategy", loop_no=1, step_no=2, action_type="call",
                capability="ghost.capability", args={}, allowed=0,
                deny_reason="能力不在菜单：ghost.capability")
    row2 = c.execute("SELECT * FROM agent_steps WHERE step_no=2 AND run_id=?",
                     (rid,)).fetchone()
    assert row2["allowed"] == 0 and "不在菜单" in row2["deny_reason"]
    # finish 步：decision_json 含 strategy 产物原文
    record_step(c, rid, mode="strategy", loop_no=1, step_no=3, action_type="finish",
                decision_json='{"action":"finish","strategy":{"priorities":[]}}',
                allowed=1)
    row3 = c.execute("SELECT * FROM agent_steps WHERE step_no=3 AND run_id=?",
                     (rid,)).fetchone()
    assert "strategy" in row3["decision_json"]


def test_reconcile_runs_closes_stale_running(conn):
    """三类残留收口：会话已 ended → 按任务态定 done/failed；队列已 finished → 同步；
    任务已不在执行态 → orphan。收口后无 running 残留。"""
    from test_agent.skills.run_ledger import finish_run, reconcile_runs, start_run
    c, tid = conn
    # ① 正常收口过的不动
    s1, r1 = start_run(c, tid, kind="single")
    finish_run(c, r1, status="done")
    # ② 会话有 ended_at（模拟 report 已写但 finish_run 漏接）→ 按任务态定
    s2, r2 = start_run(c, tid, kind="single")
    c.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?", (s2,))
    c.execute("UPDATE test_tasks SET status='failed' WHERE id=?", (tid,))
    # ③ 队列行已 finished → 同步
    s3, r3 = start_run(c, tid, kind="exec")
    qid = c.execute(
        "INSERT INTO exec_queue(task_id, kind, status, started_at, finished_at) "
        "VALUES (?, 'exec', 'done', datetime('now'), datetime('now'))", (tid,)).lastrowid
    c.execute("UPDATE agent_runs SET queue_id=? WHERE id=?", (qid, r3))
    # ④ 任务已不在执行态（ready_for_review 停等）→ orphan
    s4, r4 = start_run(c, tid, kind="d1_review")
    c.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    c.commit()
    n = reconcile_runs(c)
    assert n >= 3
    assert c.execute("SELECT COUNT(*) FROM agent_runs WHERE status='running'").fetchone()[0] == 0
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r1,)).fetchone()[0] == "done"
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r2,)).fetchone()[0] == "failed"
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r3,)).fetchone()[0] == "done"
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r4,)).fetchone()[0] == "orphan"
    assert c.execute("SELECT COUNT(*) AS n FROM agent_runs WHERE status='running'").fetchone()["n"] == 0


def test_reconcile_leaves_genuinely_running(conn):
    """任务还在执行态（running）且会话未结束 → 不误收口。"""
    from test_agent.skills.run_ledger import reconcile_runs, start_run
    c, tid = conn  # fixture 里任务状态=running
    start_run(c, tid, kind="single")
    c.commit()
    n = reconcile_runs(c)
    assert n == 0
    assert c.execute("SELECT COUNT(*) FROM agent_runs WHERE status='running'").fetchone()[0] == 1
