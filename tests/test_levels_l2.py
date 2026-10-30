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
