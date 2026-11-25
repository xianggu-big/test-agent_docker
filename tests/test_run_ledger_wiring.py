"""T3 采集接线验收（学院达标，2026-11-25）：六条全用假 LLM + tmp 库，零容器零真机。

设计稿 T3 验收六条：
1. 同一任务连续跑两次 → 两行 agent_runs、attempt_no 1/2、session 不同；第一次的
   summary_json 第二次跑完后原样可回放（终态快照防 delete-first 清史）。
2. MTA_RUN_LEDGER=0 → 两表零写入（图形态逐字节不变由既有图锁测试守护）。
3. report 后 test_reports.duration_ms 非 NULL（账本关时保持 NULL 旧行为）。
4. 固定脚本决策循环 → agent_steps 行数=循环步数；denied 步带 deny_reason；
   finish 步 decision_json 含 strategy；llm_call_id / invocation_id 能 JOIN 回原表。
5. agent_runs.prompt_version 等于当前版本常量。
6. 四类结束（正常/异常/取消/进程被杀留 running）→ reconcile 后无 running 残留。
"""
import json
import types

import pytest

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.skills import run_ledger, tool_loop
from test_agent.skills.capabilities import TaskCtx
from test_agent.skills.prompt_library import AGENT_LOOP_PROMPT_VERSION
from test_agent.skills.registry import SkillRegistry
from test_agent.store import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid
    c.commit()
    yield c, tid
    c.close()


def _on(monkeypatch):
    monkeypatch.setattr(config, "RUN_LEDGER_ENABLED", True)


def _off(monkeypatch):
    """默认已开（Q1 拍板 2026-11-25）；关态语义测试显式关闭。"""
    monkeypatch.setattr(config, "RUN_LEDGER_ENABLED", False)


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)

    def invoke(self, messages):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


def _fake_log_llm(conn):
    """log_llm_call 替身：真 INSERT llm_calls 返回真 id（验收 4 的 JOIN 用）。"""
    def log(task_id, phase, resp, **kw):
        cur = conn.execute(
            "INSERT INTO llm_calls(task_id, phase, prompt_tokens, completion_tokens,"
            " total_tokens, model, truncated) VALUES (?, ?, 10, 5, 15, 'fake', 0)",
            (task_id, phase))
        conn.commit()
        return cur.lastrowid
    return log


def _ctx(tmp_path, conn, tid, sid, rid, **snap):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.py").write_text("def add(x):\n    return x + 1\n", encoding="utf-8")
    return TaskCtx(task_id=tid, session_id=sid, workspace=ws, container_name="c",
                   primary_language="python", image="img", state_snapshot=snap,
                   conn=conn, runner=None, artifacts_dir=tmp_path / "artifacts",
                   run_id=rid)


def _llm_script_read_then_finish():
    return [
        json.dumps({"action": "call", "capability": "test.observe.read",
                    "args": {"path": "a.py", "mode": "read"}}),
        json.dumps({"action": "finish",
                    "summary": "读完了",
                    "strategy": {"priorities": [{"order": 1, "capability_id": "test.gen.cases",
                                                 "why": "证据足", "args_hint": {}}],
                                "evidence_plan": [], "fallback": "手动审查"}}),
    ]


# ---------- 验收 1：两次运行隔离 + 终态快照回放 ----------

def test_two_runs_isolated_and_summary_replayable(env, monkeypatch):
    c, tid = env
    _on(monkeypatch)
    sid1, rid1 = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    sid2, rid2 = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    assert rid1 != rid2 and sid1 != sid2
    rows = c.execute("SELECT id, attempt_no FROM agent_runs WHERE task_id=? ORDER BY id",
                     (tid,)).fetchall()
    assert [r["attempt_no"] for r in rows] == [1, 2]
    # 第一次运行收口时冻结终态快照
    run_ledger.guarded(run_ledger.finish_run, c, rid1, status="done",
                       summary_json=json.dumps(run_ledger.build_run_summary(c, tid),
                                               ensure_ascii=False))
    snap1 = c.execute("SELECT summary_json FROM agent_runs WHERE id=?", (rid1,)).fetchone()[0]
    # 第二次运行改写底层表（重跑 delete-first 的现实模拟）→ 第一次快照原样可回放
    c.execute("INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,"
              " verdict) VALUES (?, 1, 9, 9, 0, 'pass')", (tid,))
    c.execute("UPDATE test_tasks SET status='failed', error='第二次失败' WHERE id=?", (tid,))
    c.commit()
    assert c.execute("SELECT summary_json FROM agent_runs WHERE id=?",
                     (rid1,)).fetchone()[0] == snap1
    parsed = json.loads(snap1)
    assert parsed["final"]["status"] == "running"   # 快照冻结的是第一次收口时点


# ---------- 验收 2：旗标关零写入 ----------

def test_ledger_off_zero_writes(env, tmp_path, monkeypatch):
    c, tid = env
    _off(monkeypatch)
    assert config.RUN_LEDGER_ENABLED is False   # 显式关（默认开，Q1 拍板）
    sid, rid = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    assert rid is None                          # 旧行为：只有会话行
    assert c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    # ctx.run_id=None 的循环 → agent_steps 零写入（图形态逐字节不变由既有图锁测试守护）
    monkeypatch.setattr(tool_loop, "make_chat", lambda **kw: _FakeLLM(_llm_script_read_then_finish()))
    monkeypatch.setattr(tool_loop, "log_llm_call", lambda *a, **k: None)
    ctx = _ctx(tmp_path, c, tid, sid, rid=None)
    res = tool_loop.run_tool_loop(types.SimpleNamespace(registry=None), ctx, "g",
                                  "strategy", {"max_steps": 2, "max_tokens": 30000,
                                               "max_seconds": 600}, "agent_strategy")
    assert res["stop"] == "goal_met"
    assert c.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0] == 0


# ---------- 验收 3：report duration_ms ----------

def test_report_duration_non_null_when_ledger_on(env, monkeypatch):
    c, tid = env
    _on(monkeypatch)
    sid, rid = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    started = c.execute("SELECT started_ms FROM agent_runs WHERE id=?", (rid,)).fetchone()[0]
    nodes = make_nodes(GraphDeps(conn=c, runner=None, registry=None, skills={}))
    rep_state = {"task_id": tid, "session_id": sid, "verdict": "pass",
                 "passed": 3, "failed": 0, "skipped": 0, "coverage_line": 0.9,
                 "failure_kind": "none", "stop_reason": "达标", "per_file": [],
                 "job_id": 0, "module_id": None,
                 "run_id": rid, "run_started_ms": started}
    nodes["report"](rep_state)
    row = c.execute("SELECT duration_ms FROM test_reports WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    run_row = c.execute("SELECT status, summary_json FROM agent_runs WHERE id=?",
                        (rid,)).fetchone()
    assert run_row["status"] == "done"
    assert json.loads(run_row["summary_json"])["final"]["status"] == "done"


def test_report_duration_null_when_ledger_off(env, monkeypatch):
    """账本关：duration_ms=None（历史 1525 行 NULL 口径，行为逐字节不变）。"""
    c, tid = env
    _off(monkeypatch)
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES (?)", (tid,)).lastrowid
    nodes = make_nodes(GraphDeps(conn=c, runner=None, registry=None, skills={}))
    nodes["report"]({"task_id": tid, "session_id": ses, "verdict": "pass",
                     "passed": 1, "failed": 0, "skipped": 0, "coverage_line": None,
                     "per_file": [], "job_id": 0, "module_id": None})
    row = c.execute("SELECT duration_ms FROM test_reports WHERE task_id=?",
                    (tid,)).fetchone()
    assert row["duration_ms"] is None


# ---------- 验收 4：决策循环步骤行 + JOIN 回原表 ----------

def test_loop_steps_recorded_and_joinable(env, tmp_path, monkeypatch):
    c, tid = env
    _on(monkeypatch)
    sid, rid = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    monkeypatch.setattr(tool_loop, "make_chat", lambda **kw: _FakeLLM(_llm_script_read_then_finish()))
    monkeypatch.setattr(tool_loop, "log_llm_call", _fake_log_llm(c))
    ctx = _ctx(tmp_path, c, tid, sid, rid)
    deps = types.SimpleNamespace(registry=SkillRegistry(conn=c))   # 真 registry → 真 invocation 行
    res = tool_loop.run_tool_loop(deps, ctx, "g", "strategy",
                                  {"max_steps": 3, "max_tokens": 30000,
                                   "max_seconds": 600}, "agent_strategy", loop_no=1)
    assert res["stop"] == "goal_met"
    steps = c.execute("SELECT * FROM agent_steps WHERE run_id=? ORDER BY step_no",
                      (rid,)).fetchall()
    assert len(steps) == 2   # 行数=循环步数（call + finish）
    assert steps[0]["action_type"] == "call" and steps[0]["allowed"] == 1
    assert steps[1]["action_type"] == "finish"
    assert "strategy" in steps[1]["decision_json"]
    # 外键 JOIN 回原表（防双轨验证）
    join_llm = c.execute(
        "SELECT s.step_no, l.phase FROM agent_steps s JOIN llm_calls l "
        "ON l.id = s.llm_call_id WHERE s.run_id=?", (rid,)).fetchall()
    assert len(join_llm) == 2 and all(r["phase"] == "agent_strategy" for r in join_llm)
    join_inv = c.execute(
        "SELECT si.status FROM agent_steps s JOIN skill_invocations si "
        "ON si.id = s.invocation_id WHERE s.run_id=?", (rid,)).fetchall()
    assert len(join_inv) == 1 and join_inv[0]["status"] == "ok"


def test_denied_step_has_deny_reason(env, tmp_path, monkeypatch):
    c, tid = env
    _on(monkeypatch)
    sid, rid = run_ledger.ledger_start_run(c, tid, kind="single", goal_text="g")
    script = [json.dumps({"action": "call", "capability": "ghost.capability", "args": {}}),
              json.dumps({"action": "finish", "summary": "s",
                          "strategy": {"priorities": [], "evidence_plan": [], "fallback": "f"}})]
    monkeypatch.setattr(tool_loop, "make_chat", lambda **kw: _FakeLLM(script))
    monkeypatch.setattr(tool_loop, "log_llm_call", _fake_log_llm(c))
    ctx = _ctx(tmp_path, c, tid, sid, rid)
    tool_loop.run_tool_loop(types.SimpleNamespace(registry=None), ctx, "g", "strategy",
                            {"max_steps": 2, "max_tokens": 30000, "max_seconds": 600},
                            "agent_strategy", loop_no=1)
    denied = c.execute("SELECT deny_reason, allowed FROM agent_steps WHERE run_id=? "
                       "AND step_no=1", (rid,)).fetchone()
    assert denied["allowed"] == 0 and "能力不在菜单" in denied["deny_reason"]


# ---------- 验收 5：prompt_version 常量 ----------

def test_prompt_version_constant(env):
    c, tid = env
    from test_agent.skills.run_ledger import start_run   # 直调纯函数（不经旗标）
    sid, rid = start_run(c, tid, kind="single", goal_text="g",
                         prompt_version=AGENT_LOOP_PROMPT_VERSION)
    row = c.execute("SELECT prompt_version FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert row["prompt_version"] == AGENT_LOOP_PROMPT_VERSION


# ---------- 验收 6：四类结束 → reconcile 无 running 残留 ----------

def test_reconcile_after_four_endings(env):
    c, tid = env
    from test_agent.skills.run_ledger import finish_run, reconcile_runs, start_run
    # ① 正常：finish_run 收口 done
    _, r1 = start_run(c, tid, kind="single")
    finish_run(c, r1, status="done")
    # ② 异常上抛（builder handler 收口 failed——此处模拟漏接后 reconcile 兜底：
    #    会话被 handler 关了，run 行没来得及收）
    _, r2 = start_run(c, tid, kind="single")
    c.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id="
              "(SELECT session_id FROM agent_runs WHERE id=?)", (r2,))
    c.execute("UPDATE test_tasks SET status='failed' WHERE id=?", (tid,))
    # ③ 用户取消：任务 cancelled（stop API 只改任务与队列行，run 行靠 reconcile）
    _, r3 = start_run(c, tid, kind="exec")
    qid = c.execute("INSERT INTO exec_queue(task_id, kind, status, started_at,"
                    " finished_at) VALUES (?, 'exec', 'cancelled', datetime('now'),"
                    " datetime('now'))", (tid,)).lastrowid
    c.execute("UPDATE agent_runs SET queue_id=? WHERE id=?", (qid, r3))
    c.execute("UPDATE test_tasks SET status='cancelled' WHERE id=?", (tid,))
    # ④ 进程被杀：什么都没留，任务还在执行态 → 转孤儿前先确认"仍在执行"不误收；
    #    真实进程被杀后任务态最终非执行态（worker 死前任务卡 running——此处模拟
    #    worker 恢复后把任务标 failed 的场景之外：任务仍在 running → 不动它）
    _, r4 = start_run(c, tid, kind="single")
    c.execute("UPDATE test_tasks SET status='running' WHERE id=?", (tid,))
    c.commit()
    assert reconcile_runs(c) >= 2   # ②③ 收口
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r1,)).fetchone()[0] == "done"
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r2,)).fetchone()[0] == "failed"
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r3,)).fetchone()[0] == "cancelled"
    # ④ 任务仍执行态 → 保持 running（不误杀真实运行）；QueueWorker.start 每次启动
    # 都会再 reconcile，任务终态落库后自然收口
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (r4,)).fetchone()[0] == "running"


def test_process_killed_chain_closes_run(env):
    """验收⑥第④类字面链路：进程被杀 → worker 重启 _recover 把队列行 running 标
    failed → reconcile 按队列状态收口 run 行。语义边界（R1 审查注记）：若任务仍在
    真实执行态（worker 活着正跑），reconcile 保持 running 不误杀——见上一测试④。"""
    c, tid = env
    from test_agent.skills.run_ledger import reconcile_runs, start_run
    _, rid = start_run(c, tid, kind="exec")
    qid = c.execute("INSERT INTO exec_queue(task_id, kind, status, started_at) "
                    "VALUES (?, 'exec', 'running', datetime('now'))", (tid,)).lastrowid
    c.execute("UPDATE agent_runs SET queue_id=? WHERE id=?", (qid, rid))
    c.execute("UPDATE test_tasks SET status='running' WHERE id=?", (tid,))
    c.commit()
    # 进程被杀后 QueueWorker.start：先 _recover（队列行 running→failed），再 reconcile
    c.execute("UPDATE exec_queue SET status='failed', finished_at=datetime('now'), "
              "error='进程重启恢复' WHERE id=?", (qid,))
    c.commit()
    n = reconcile_runs(c)
    assert n == 1
    assert c.execute("SELECT status FROM agent_runs WHERE id=?", (rid,)).fetchone()[0] == "failed"
