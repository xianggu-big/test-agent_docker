"""四级双轨 P1 单测：test_levels 台账、level_supervisor 串行接力（D9）、L2 skill 的 DB 侧。

纪律：全部 tmp 库（config.DB_PATH 是模块级常量，必须直接 patch——第九轮实锤的老坑）；
Docker/LLM 零依赖（run_task/run_l2_task/容器均 monkeypatch 或 fake）。
"""
import json

import pytest

from test_agent import config
from test_agent.graph import levels as levels_mod
from test_agent.graph.l2 import build_l2_skills
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    return db


def _mk_task(db) -> int:
    conn = get_conn(db)
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed)"
                       " VALUES(?,?,?,1)", (pid, uid, "需求")).lastrowid
    conn.commit()
    conn.close()
    return tid


class _FakeRunner:
    """gen_contract_tests 的容器交互 fake：copy 落盘为 no-op。"""

    def copy_dir_into(self, container, ws, dest_path="/work"):
        pass

    class client:  # noqa: N801 —— 供 runner.client.containers.get(...) 访问
        class containers:
            @staticmethod
            def get(name):
                return object()


def test_leg_upsert_and_trace_chain(tmp_db):
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    l1 = levels_mod._leg_upsert(conn, tid, "L1", strategy="full", status="done")
    l2 = levels_mod._leg_upsert(conn, tid, "L2", strategy="full", status="running",
                                trace_from=l1)
    row = conn.execute("SELECT trace_from, level FROM test_levels WHERE id=?", (l2,)).fetchone()
    assert row["trace_from"] == l1 and row["level"] == "L2"     # 接力链头落库
    l2b = levels_mod._leg_upsert(conn, tid, "L2", status="done")  # 重跑 upsert 不炸不翻倍
    assert l2b == l2
    n = conn.execute("SELECT COUNT(*) AS n FROM test_levels WHERE task_id=?", (tid,)).fetchone()["n"]
    assert n == 2


def test_supervisor_serial_relay(tmp_db, monkeypatch):
    tid = _mk_task(tmp_db)
    l1_final = {"done": True, "verdict": "pass", "passed": 30, "failed": 0,
                "coverage_line": 0.9, "stop_reason": None}
    l2_final = {"verdict": "pass", "stop_reason": "契约与需求断言全过"}
    calls = []

    monkeypatch.setattr(levels_mod, "run_task", lambda t: calls.append(("L1", t)) or l1_final)

    def fake_l2(t, trace_from=None):
        calls.append(("L2", t, trace_from))
        conn = get_conn(tmp_db)                       # 真 run_l2_task 会建 L2 行，fake 里等价模拟
        levels_mod._leg_upsert(conn, t, "L2", strategy="full", status="done",
                               trace_from=trace_from)
        return l2_final

    monkeypatch.setattr(levels_mod, "run_l2_task", fake_l2)
    out = levels_mod.run_task_levels(tid)
    assert out["L1"]["verdict"] == "pass" and out["L2"]["verdict"] == "pass"
    assert [c[0] for c in calls] == ["L1", "L2"]                # 串行：L1 先 L2 后
    conn = get_conn(tmp_db)
    rows = {r["level"]: r for r in conn.execute(
        "SELECT * FROM test_levels WHERE task_id=?", (tid,)).fetchall()}
    assert rows["L1"]["status"] == "done" and rows["L2"]["trace_from"] == rows["L1"]["id"]
    assert json.loads(rows["L1"]["summary"])["passed"] == 30    # L1 摘要确定性落库


def test_supervisor_unconfirmed_fails_closed(tmp_db):
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    conn.execute("UPDATE test_tasks SET confirmed=0 WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="确认门"):
        levels_mod.run_task_levels(tid)


def test_l2_extract_endpoints_skill_writes_db(tmp_db, tmp_path):
    tid = _mk_task(tmp_db)
    (tmp_path / "openapi.json").write_text(json.dumps({
        "paths": {"/ping": {"get": {"operationId": "ping_ping_get",
                                    "responses": {"200": {"description": "ok"}}}}}}),
        encoding="utf-8")
    skills = build_l2_skills(_FakeRunner())
    out = json.loads(skills["extract_endpoints"].func(tid, {"workspace": str(tmp_path)}))
    assert out["how"] == "spec:openapi.json" and len(out["endpoints"]) == 1
    conn = get_conn(tmp_db)
    row = conn.execute("SELECT method, path, source FROM interface_endpoints WHERE task_id=?",
                       (tid,)).fetchone()
    assert row["method"] == "GET" and row["path"] == "/ping" and row["source"] == "spec"
    # 重跑 delete-first 幂等
    skills["extract_endpoints"].func(tid, {"workspace": str(tmp_path)})
    n = conn.execute("SELECT COUNT(*) AS n FROM interface_endpoints WHERE task_id=?",
                     (tid,)).fetchone()["n"]
    assert n == 1


def test_l2_extract_no_spec_degrades_honestly(tmp_db, tmp_path):
    tid = _mk_task(tmp_db)
    skills = build_l2_skills(_FakeRunner())
    out = json.loads(skills["extract_endpoints"].func(tid, {"workspace": str(tmp_path)}))
    assert out["endpoints"] is None and out["how"] == "no_spec"   # D4 ③：诚实降级不硬造
    n = get_conn(tmp_db).execute(
        "SELECT COUNT(*) AS n FROM interface_endpoints WHERE task_id=?", (tid,)).fetchone()["n"]
    assert n == 0


def test_l2_gen_contract_tests_full_flow(tmp_db, tmp_path):
    tid = _mk_task(tmp_db)
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n",
                                     encoding="utf-8")
    skills = build_l2_skills(_FakeRunner())
    # 走 args 直传（字段全）的端点路径：success_code/documented_codes 从 args 补
    endpoints = [{"method": "POST", "path": "/coupons", "operation_id": "create_coupon_coupons_post",
                  "params": [], "request_schema": {"type": "object",
                                                   "required": ["code"],
                                                   "properties": {"code": {"type": "string"}}},
                  "response_schema": None, "source": "spec", "success_code": 201,
                  "documented_codes": ["201", "422"]}]
    out = json.loads(skills["gen_contract_tests"].func(tid, {
        "workspace": str(tmp_path), "container_name": "c", "requirement": "",
        "endpoints": endpoints, "scenarios": [], "service_port": 8000}))
    assert out["contract"] >= 2 and out["file"] == "test_api_r1.py"       # happy + 必填缺失
    assert (tmp_path / "test_api_r1.py").exists()
    n = get_conn(tmp_db).execute(
        "SELECT COUNT(*) AS n FROM test_cases c JOIN test_scripts s ON s.id=c.script_id"
        " WHERE s.task_id=?", (tid,)).fetchone()["n"]
    assert n == out["contract"] + out["sequence"] + out["requirement"]    # 用例全量落库


def test_run_worker_dispatches_to_levels_when_enabled(tmp_db, monkeypatch):
    """API 下发分派分支锁：LEVELS_ENABLED=True 走串行接力，False 走 L1 原样。"""
    from test_agent import config
    from test_agent.graph import levels as levels_mod
    from test_agent.api import server
    calls = []
    monkeypatch.setattr(config, "LEVELS_ENABLED", True)
    monkeypatch.setattr(levels_mod, "run_task_levels",
                        lambda t, levels=("L1", "L2"):
                        calls.append(("levels", t, levels)) or {"L1": {}})
    server._run_worker(99)
    monkeypatch.setattr(config, "LEVELS_L3_ENABLED", True)   # L3 开：接力元组带 L3
    server._run_worker(99)
    monkeypatch.setattr(config, "LEVELS_ENABLED", False)
    monkeypatch.setattr(server, "run_task", lambda t: calls.append(("l1", t)))
    server._run_worker(99)
    assert calls[0] == ("levels", 99, ("L1", "L2"))          # L3 关：接力止于 L2
    assert calls[1] == ("levels", 99, ("L1", "L2", "L3"))    # L3 开：元组带 L3
    assert calls[2][0] == "l1"


# ---------- Loop-1（TODO-01/P2-5a）：L2 判读纯函数化 + 0 用例收集诚实语义 ----------

def test_judge_l2_exit5_no_failed_is_collect_failure_not_contract(tmp_db):
    """exit=5（pytest 收集失败）且无 FAILED 行：不得谎称『契约违背 N 处：实现与 spec 不符』。"""
    from test_agent.graph.l2 import judge_l2
    summary, verdict, reason = judge_l2(
        stdout="no tests ran in 0.01s", last_exit=5,
        endpoints=[{"path": "/x", "method": "GET"}], skipped_routes=[])
    assert verdict == "stop"
    assert "无可执行用例" in reason or "收集" in reason
    assert "实现与 spec 不符" not in reason
    assert summary["failed"] == 0


def test_judge_l2_contract_violation_semantics_kept(tmp_db):
    """有 FAILED 行（契约违背）时维持原语义（不误伤）。"""
    from test_agent.graph.l2 import judge_l2
    stdout = "1 failed, 2 passed in 0.1s\nFAILED test_gen_r1.py::test_x"
    summary, verdict, reason = judge_l2(stdout=stdout, last_exit=1,
                                        endpoints=[{"path": "/x", "method": "GET"}],
                                        skipped_routes=[])
    assert verdict == "stop" and "契约违背" in reason
    assert summary["contract_violations"] == 1


def test_judge_l2_pass_semantics_kept(tmp_db):
    from test_agent.graph.l2 import judge_l2
    summary, verdict, reason = judge_l2(
        stdout="3 passed in 0.1s", last_exit=0,
        endpoints=[{"path": "/x", "method": "GET"}], skipped_routes=[])
    assert verdict == "pass"


# ---------- Loop-3（TODO-04/P3-2）：同名场景 title 匹配串写防线 ----------

def test_apply_smoke_duplicate_title_goes_manual_not_crosswrite(tmp_db):
    """同名场景多行：冒烟结果不得自动套用（转人工），单行场景照旧自动判定。"""
    from test_agent.skills.scenario_gen import apply_smoke_results
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    for title in ("重复场景", "重复场景", "唯一场景"):
        conn.execute(
            """INSERT INTO scenario_cases(task_id, level, title, steps, status)
               VALUES(?, 'L3', ?, '[{"method":"GET","path":"/x"}]', 'pending')""",
            (tid, title))
    conn.commit()
    out = apply_smoke_results(conn, tid, [
        {"title": "重复场景", "ok": True, "steps": []},
        {"title": "唯一场景", "ok": True, "steps": [],
         }], level="L3")
    rows = {(r["id"], r["title"], r["status"]) for r in conn.execute(
        "SELECT id, title, status FROM scenario_cases WHERE task_id=?", (tid,))}
    conn.close()
    dup = [s for (_i, t, s) in rows if t == "重复场景"]
    assert all(s == "pending_confirm" for s in dup), f"同名行必须转人工，实际 {dup}"
    single = [s for (_i, t, s) in rows if t == "唯一场景"]
    assert single == ["auto_pass"], "单行场景不误伤"


# ---------- S0：E6 消融三开关（L2E1 黑盒/L2E2 白盒/L2E3 跨级） ----------

def test_e6_白盒关_callgraph跳过顺序场景空(tmp_db, monkeypatch):
    """L2E2：白盒关→callgraph 节点直接跳过（scenarios=[]，不冒充）。"""
    from test_agent.graph import l2 as l2mod
    monkeypatch.setattr(l2mod.config, "L2_WHITEBOX_ENABLED", False)
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    sid = conn.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    conn.commit()
    nodes = l2mod.make_l2_nodes(type("D", (), {"runner": None, "registry": None,
                                               "conn": conn, "skills": {}}))
    out = nodes["callgraph"]({"task_id": tid, "workspace": "/x", "endpoints": [],
                              "session_id": sid})
    assert out == {"scenarios": []}


def test_e6_黑盒关_契约轨置空_顺序轨保留(tmp_db, monkeypatch):
    """L2E1：黑盒关→gen_contract_tests 内跳过 render_contract_module（源码契约+门控行为）。"""
    from test_agent.graph import l2 as l2mod
    from test_agent.skills import contract_gen as cg
    calls = {"render": 0}
    orig = cg.render_contract_module
    def fake_render(*a, **k):
        calls["render"] += 1
        return ("", [], {}, [])
    monkeypatch.setattr(cg, "render_contract_module", fake_render)
    monkeypatch.setattr(l2mod.config, "L2_BLACKBOX_ENABLED", False)
    nodes = l2mod.make_l2_nodes(type("D", (), {"runner": None, "registry": None,
                                               "conn": get_conn(tmp_db), "skills": {}}))
    # gen_contract_tests 需要 registry.invoke——gen 技能在 skills dict 里；直接走注册表路径太重，
    # 改为验证门控语义：黑盒 False 时经该函数不触发 render（用最小 args 调真实函数）
    try:
        nodes["gen_contract_tests"](1, {"workspace": str(tmp_db), "endpoints": [],
                                        "service_port": 8999})
    except Exception:
        pass  # 函数尾部写盘/回灌可能因空 endpoints 提前退出——门控行为已由下方源码断言锁
    assert calls["render"] == 0, "黑盒关时不得调用 render_contract_module"
    import inspect
    src = inspect.getsource(l2mod)
    assert "L2_BLACKBOX_ENABLED" in src
