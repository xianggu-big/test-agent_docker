"""第四轮修复锁（2026-11-15 闲时找bug 第四轮，R13 状态机迁移 + R15 比率口径维度）。

实锤四处：
1. run_task_prep 把 static_draft 的成功完成（done=True, verdict=pass）当"analyze
   诚实降级"标 failed——两阶段 static_only（Tier3）项目阶段一永远 failed，评审页
   不可达（图注释与 docstring 都承诺"静态模式停在待审查"；analyze 598 行起已不
   再 done 短路，prep 图里唯一 done 生产者是 static_draft）。
2. 批量 auto-run 不校验 two_stage——单阶段任务被拉进两阶段停等点，run_task_exec
   撞"任务未开启两阶段流程"守卫后滞留 ready_for_review，放行又走全图（审查结论作废）。
3. api_stop 对 done/failed/ready_for_review 返回 stopped:true 但零状态变化——
   谎报成功（queued 分支的 UPDATE WHERE status='queued' 不命中仍返回 true）。
4. review_rules 通过率白名单只认 L1 pass_rate——gen_report 指标包含 l3.pass_rate，
   叙述如实引用"L3 场景通过率 60%"会被误判造假 reject。
"""
import types

import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        yield client, h, db.get_conn(), tmp_path


def _mk_task(c, **kw):
    uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    cols = {"status": "queued", "confirmed": 1, "two_stage": 1, "ai_reviewer": "off"}
    cols.update(kw)
    keys = ",".join(k for k in cols if k in ("status", "confirmed", "two_stage", "ai_reviewer"))
    vals = [cols[k] for k in keys.split(",")]
    return c.execute(
        f"INSERT INTO test_tasks(project_id,user_id,coverage_target,{keys}) "
        f"VALUES(?,?,0.7,{','.join('?' * len(vals))})", (pid, uid, *vals)).lastrowid


# ---------- 1. prep 静态草稿停待审查 ----------

class _FakeGraph:
    def __init__(self, final):
        self._final = final

    def invoke(self, init, cfg):
        return self._final


class _FakeRunner:
    def cleanup(self, name):
        pass


def _prep_with(monkeypatch, final):
    from test_agent.graph import builder
    from test_agent.store import db
    monkeypatch.setattr(builder, "build_prep_graph", lambda deps: _FakeGraph(final))
    monkeypatch.setattr(builder, "make_deps",
                        lambda: types.SimpleNamespace(conn=db.get_conn(),
                                                      runner=_FakeRunner()))
    return builder


def test_prep_static_draft_lands_ready_for_review(env, monkeypatch):
    """static_draft done=True verdict=pass（草稿已落库）= 阶段一成功——停待审查，
    不是 failed。"""
    client, h, c, _ = env
    tid = _mk_task(c, two_stage=1)
    c.commit()
    builder = _prep_with(monkeypatch, {"done": True, "verdict": "pass",
                                       "stop_reason": "静态模式：确定性扫描与设计草稿已落库"})
    builder.run_task_prep(tid)
    st = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()["status"]
    assert st == "ready_for_review", "静态草稿成功被当'诚实降级'标 failed——评审页永不可达"


def test_prep_degrade_short_circuit_still_fails(env, monkeypatch):
    """done 短路且无 pass/stop verdict（真·诚实降级形态）仍如实 failed（防回归）。"""
    client, h, c, _ = env
    tid = _mk_task(c, two_stage=1)
    c.commit()
    builder = _prep_with(monkeypatch, {"done": True, "stop_reason": "准备阶段诚实降级终止"})
    builder.run_task_prep(tid)
    st = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()["status"]
    assert st == "failed"


# ---------- 2. 批量 auto-run 校验 two_stage ----------

def test_batch_auto_run_rejects_single_stage(env):
    client, h, c, _ = env
    tid = _mk_task(c, two_stage=0, ai_reviewer="auto", confirmed=0)
    c.commit()
    r = client.post("/api/tasks/batch/auto-run", headers=h, json={"task_ids": [tid]})
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["started"] is False and "两阶段" in res["reason"], \
        "单阶段任务进 auto 链会滞留 ready_for_review（run_task_exec 守卫拒绝）"
    q = c.execute("SELECT COUNT(*) AS n FROM exec_queue").fetchone()["n"]
    assert q == 0, "被拒任务不得入队"


def test_batch_auto_run_two_stage_still_works(env):
    client, h, c, _ = env
    tid = _mk_task(c, two_stage=1, ai_reviewer="auto", confirmed=0)
    c.commit()
    r = client.post("/api/tasks/batch/auto-run", headers=h, json={"task_ids": [tid]})
    res = r.json()["results"][0]
    assert res["started"] is True


# ---------- 3. stop 不再谎报 ----------

def test_stop_terminal_task_reports_not_stopped(env):
    client, h, c, _ = env
    tid = _mk_task(c, status="done")
    c.commit()
    r = client.post(f"/api/tasks/{tid}/stop", headers=h)
    assert r.status_code == 200
    assert r.json()["stopped"] is False, "终态任务 stop 谎报成功（零状态变化）"


def test_stop_ready_for_review_reports_not_stopped(env):
    client, h, c, _ = env
    tid = _mk_task(c, status="ready_for_review")
    c.commit()
    r = client.post(f"/api/tasks/{tid}/stop", headers=h)
    assert r.json()["stopped"] is False


def test_stop_queued_still_stops(env):
    client, h, c, _ = env
    tid = _mk_task(c, status="queued")
    c.commit()
    r = client.post(f"/api/tasks/{tid}/stop", headers=h)
    assert r.json()["stopped"] is True


# ---------- 4. review_rules 认 L3 场景通过率 ----------

def test_review_rules_accepts_l3_scenario_rate():
    from test_agent.skills.evaluate import review_rules
    metrics = {"passed": 10, "failed": 0, "skipped": 0, "coverage_line": 0.9,
               "pass_rate": 1.0, "l3": {"pass_rate": 0.6, "total": 4, "auto_pass": 2}}
    assert review_rules("L3 场景通过率 60%，其中 2 个待人工确认。", metrics, "pass") == [], \
        "指标包含 l3.pass_rate=0.6——叙述如实引用被误判造假"


def test_review_rules_still_rejects_fabricated_rate():
    from test_agent.skills.evaluate import review_rules
    metrics = {"passed": 10, "failed": 0, "skipped": 0, "coverage_line": 0.9,
               "pass_rate": 1.0, "l3": {"pass_rate": 0.6, "total": 4, "auto_pass": 2}}
    assert review_rules("通过率 75%。", metrics, "pass") != [], \
        "75% 不匹配任何已知指标——仍必须拦（防造假闸不放松）"
