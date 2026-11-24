"""T4 四指标计算与 API 回归（学院达标，2026-11-25）。

覆盖学院进度文档 P0-4 的七条 + 补充三条：
1. 同一任务多次运行的 run_id 隔离；
2. 重跑不覆盖历史运行（快照回放）；
3. 完成率公式及环境失败边界（含零分母/取消）；
4. 工具成功率包含 failed/timeout/denied（分母=全部发起）；
5. 幻觉标注与证据引用完整性（C1 序号越界/C2 编造能力=机器可核对）；
6. 响应时间起止点和零分母处理；
7. Agent 轨迹 API 不跨运行串账；
8. 旗标关态 API 诚实空态（ledger_enabled:false）；
9. 历史任务（无 run 行）不 500（legacy 近似 or None）；
10. 零样本返回 null 不返回 0。
"""
import json
import types

import pytest
from fastapi.testclient import TestClient

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.skills import run_ledger, run_metrics, tool_loop
from test_agent.skills.capabilities import TaskCtx
from test_agent.skills.registry import SkillRegistry
from test_agent.store import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid
    c.commit()
    yield c, tid, uid, pid
    c.close()


@pytest.fixture()
def client(env, monkeypatch):
    c, tid, uid, pid = env
    from test_agent.api import server, auth
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        yield client, headers, c, tid


def _make_run(c, tid, kind="single", status="done", summary=None, stop_reason=None):
    sid, rid = run_ledger.start_run(c, tid, kind=kind, goal_text="g")
    run_ledger.finish_run(c, rid, status=status, stop_reason=stop_reason,
                          summary_json=json.dumps(summary, ensure_ascii=False)
                          if summary is not None else None)
    return rid


def _summary(passed=3, failed=0, kind="none", verdict="pass", stop="达标",
             task_status="done"):
    return {"report": [{"passed": passed, "failed": failed, "skipped": 0,
                        "coverage_line": 0.9,
                        "defects": json.dumps({"failure_kind": kind,
                                               "stop_reason": stop})}],
            "case_counts": {"passed": passed, "failed": failed, "error": 0,
                            "skipped": 0, "total": passed + failed},
            "final": {"status": task_status, "error": None},
            "verdict": verdict,
            "evaluation_rounds": [], "loop_msgs": []}


# ---------- P0-4 ①②：隔离与重跑不覆盖（wiring 文件已测快照回放，这里测 API 面） ----------

def test_api_task_runs_grouped_by_attempt(client, monkeypatch):
    client_, headers, c, tid = client
    monkeypatch.setattr(config, "RUN_LEDGER_ENABLED", True)
    _make_run(c, tid, "single", summary=_summary())
    _make_run(c, tid, "single", summary=_summary())
    _make_run(c, tid, "exec", summary=_summary())
    r = client_.get(f"/api/tasks/{tid}/runs", headers=headers)
    assert r.status_code == 200 and r.json()["ledger_enabled"] is True
    attempts = r.json()["attempts"]
    # single/single 各开新尝试（1、2）；exec 归入最新尝试（2）——契约 §0
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    kinds = [run["kind"] for run in attempts[1]["runs"]]
    assert kinds == ["single", "exec"]


# ---------- P0-4 ③：完成率公式与环境失败边界 ----------

def test_classify_outcome_matrix():
    """契约 §1.1 全分支：取消/env/零执行/检出/诚实出口/熔断兜底。"""
    F = run_metrics.classify_outcome
    assert F(run_status="cancelled", task_status="cancelled", failure_kind=None,
             verdict=None) == "not_completed_user"
    assert F(run_status="failed", task_status="failed", failure_kind="env",
             verdict="stop") == "not_completed_env"
    assert F(run_status="failed", task_status="failed", failure_kind="case_defect",
             verdict="stop", passed=0, failed=0) == "not_completed_agent"
    assert F(run_status="failed", task_status="failed", failure_kind="product_defect",
             verdict="stop", passed=2, failed=3, has_report=True) == "completed"
    assert F(run_status="done", task_status="done", failure_kind="none",
             verdict="pass", passed=5, failed=0, has_report=True) == "completed"
    assert F(run_status="failed", task_status="failed", failure_kind="static",
             verdict="stop", has_report=True) == "completed_with_limitation"
    assert F(run_status="failed", task_status="failed", failure_kind="none",
             verdict="stop", passed=4, failed=0, has_report=True,
             stop_reason="API 型黑盒覆盖率不可达（用例真 HTTP…）") == "completed_with_limitation"
    # 熔断未获结论（env/none 之外不可判诚实）→ agent 责任侧
    assert F(run_status="failed", task_status="failed", failure_kind="none",
             verdict="stop", passed=1, failed=1, has_report=True,
             stop_reason="同指纹熔断：失败模式重复 3 次") == "not_completed_agent"
    assert F(run_status="orphan", task_status="running", failure_kind=None,
             verdict=None) == "not_completed_agent"


def test_api_summary_completion_rates_and_zero_denominator(client, monkeypatch):
    """完成率三口径 + 取消不进分母 + 零样本 null。"""
    client_, headers, c, tid = client
    monkeypatch.setattr(config, "RUN_LEDGER_ENABLED", True)
    _make_run(c, tid, summary=_summary(passed=5, failed=0, verdict="pass"))          # completed
    _make_run(c, tid, summary=_summary(kind="env", verdict="stop", stop="Go 工具链闸",
                                       task_status="failed"), status="failed")       # not_completed_env
    _make_run(c, tid, summary=_summary(passed=2, failed=3, kind="product_defect",
                                       verdict="stop", stop="疑似产品缺陷",
                                       task_status="failed"), status="failed")       # completed
    r = client_.get("/api/metrics/summary", headers=headers)
    m = r.json()["metrics"]
    assert m["outcomes"]["completed"] == 2
    assert m["outcomes"]["not_completed_env"] == 1
    # D=3：严格=2/3；含降级=2/3；排环境=2/2=1.0
    assert m["completion"]["denominator"] == 3
    assert abs(m["completion"]["strict"] - 2 / 3) < 1e-9
    assert abs(m["completion"]["valid_excl_env"] - 1.0) < 1e-9
    # 全取消 → 分母 0 → null
    c.execute("DELETE FROM agent_runs")
    c.commit()
    _make_run(c, tid, status="cancelled")
    r = client_.get("/api/metrics/summary", headers=headers)
    m = r.json()["metrics"]
    assert m["completion"]["denominator"] == 0
    assert m["completion"]["strict"] is None


# ---------- P0-4 ④：工具成功率分母含 failed/timeout/denied ----------

def test_tool_metrics_denominator_includes_all_outcomes(env):
    c, tid, uid, pid = env
    sid, rid = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    steps = [
        ("call", 1, "ok", None),                       # ok
        ("call", 1, "failed", None),                   # failed
        ("call", 1, "timeout", None),                  # timeout
        ("call", 0, None, "能力不在菜单：ghost"),       # denied_policy（编造）
        ("call", 0, None, "参数校验拒绝：缺少必填键"),  # denied_policy（schema）
        ("unparseable", None, None, None),             # parse_error
        ("call", None, None, "预算/重复闸停止：budget"),  # not_executed
    ]
    for i, (at, allowed, status, deny) in enumerate(steps, 1):
        run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=i,
                               action_type=at, capability="test.observe.read",
                               allowed=allowed, result_status=status, deny_reason=deny)
    m = run_metrics.tool_metrics_for_run(c, rid)
    assert m["calls"] == 7
    assert m["counts"] == {"ok": 1, "failed": 1, "timeout": 1, "denied_policy": 2,
                           "denied_registry": 0, "parse_error": 1, "not_executed": 1}
    assert abs(m["call_success_rate"] - 1 / 7) < 1e-9      # 分母=全部发起
    assert abs(m["exec_success_rate"] - 1 / 3) < 1e-9      # 辅口径=执行类
    assert m["denied_by_fabrication"] == 1                 # 编造子类
    agg = run_metrics.tool_metrics_aggregate(c, [rid])
    assert agg["micro"]["counts"]["ok"] == 1 and agg["n_zero_call_runs"] == 0


# ---------- P0-4 ⑤：幻觉标注与证据引用完整性 ----------

def test_hallucination_checks_c1_c2(env):
    c, tid, uid, pid = env
    sid, rid = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=1,
                           action_type="call", capability="test.observe.read",
                           allowed=1, result_status="ok")
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=2,
                           action_type="call", capability="ghost.cap", args={},
                           allowed=0, deny_reason="能力不在菜单：ghost.cap")
    run_ledger.record_step(c, rid, mode="diagnose", loop_no=101, step_no=3,
                           action_type="finish", allowed=1,
                           decision_json=json.dumps({
                               "action": "finish",
                               "diagnosis": {"kind": "env_error", "confidence": "low",
                                             "evidence_refs": [1, 99],   # 99 越界
                                             "reason": "r", "suggested_action": "env_fix"}}))
    h = run_metrics.hallucination_checks_for_run(c, rid)
    assert h["c1"] == {"total": 2, "invalid": 1}       # ref 99 越界（有效步只有 1、2）
    assert h["c2"] == {"total": 2, "invalid": 1}       # ghost.cap 编造
    assert abs(h["checkable_hallucination_rate"] - 2 / 4) < 1e-9


# ---------- P0-4 ⑥：响应时间起止点与零分母 ----------

def test_response_time_and_summarize_zero_sample(env):
    c, tid, uid, pid = env
    sid, rid = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    assert run_metrics.response_time_for_run(c, c.execute(
        "SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()) is None   # 未收口→缺失
    run_ledger.finish_run(c, rid, status="done")
    rt = run_metrics.response_time_for_run(c, c.execute(
        "SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone())
    assert rt is not None and rt["exec_ms"] >= 0
    assert run_metrics.summarize([]) is None                 # 零样本无缺失→None
    s = run_metrics.summarize([], missing=2)                 # 零样本有缺失→n=0+缺失率
    assert s["n"] == 0 and s["missing"] == 2
    st = run_metrics.summarize([100.0, 200.0, 300.0, 400.0])
    assert st["p50"] == 250.0 and st["p95"] > 300.0 and st["n"] == 4


# ---------- P0-4 ⑦：轨迹 API 不跨运行串账 ----------

def test_api_run_detail_no_cross_run_leak(client, monkeypatch, env):
    client_, headers, c, tid = client
    monkeypatch.setattr(config, "RUN_LEDGER_ENABLED", True)
    _, rid1 = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    run_ledger.record_step(c, rid1, mode="strategy", loop_no=1, step_no=1,
                           action_type="finish", allowed=1,
                           decision_json=json.dumps({"action": "finish", "summary": "run1"}))
    _, rid2 = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    run_ledger.record_step(c, rid2, mode="strategy", loop_no=1, step_no=1,
                           action_type="finish", allowed=1,
                           decision_json=json.dumps({"action": "finish", "summary": "run2"}))
    d1 = client_.get(f"/api/runs/{rid1}", headers=headers).json()
    d2 = client_.get(f"/api/runs/{rid2}", headers=headers).json()
    assert len(d1["steps"]) == 1 and "run1" in d1["steps"][0]["decision_json"]
    assert len(d2["steps"]) == 1 and "run2" in d2["steps"][0]["decision_json"]
    assert all(s["run_id"] == rid1 for s in d1["steps"])
    assert all(s["run_id"] == rid2 for s in d2["steps"])


# ---------- 补充 8：旗标关态 API 诚实空态 ----------

def test_apis_honest_when_ledger_off(client):
    client_, headers, c, tid = client
    assert config.RUN_LEDGER_ENABLED is False
    for url in ("/api/runs", f"/api/tasks/{tid}/runs", "/api/metrics/summary"):
        r = client_.get(url, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ledger_enabled"] is False
        assert body["runs"] == []
    r = client_.get("/api/runs/999999", headers=headers)
    assert r.json()["ledger_enabled"] is False


# ---------- 补充 9：历史任务（无 run 行）不 500 ----------

def test_legacy_task_no_500(env):
    c, tid, uid, pid = env
    # 无 run 行的任务：legacy 近似返回 None（会话缺 ended_at）或 dict；都不炸
    assert run_metrics._legacy_response_time(c, tid) is None
    c.execute("INSERT INTO agent_sessions(task_id, started_at, ended_at) "
              "VALUES (?, datetime('now','-60 seconds'), datetime('now'))", (tid,))
    c.commit()
    legacy = run_metrics._legacy_response_time(c, tid)
    assert legacy is not None and legacy["approx"] is True and legacy["exec_s"] >= 59


# ---------- 补充 10：outcome 层零样本（running/None 不进统计） ----------

def test_outcome_none_for_running_and_summary_null(env):
    c, tid, uid, pid = env
    sid, rid = run_ledger.start_run(c, tid, kind="single", goal_text="g")   # 不收口
    row = c.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()
    assert run_metrics.outcome_for_run(c, row) is None
