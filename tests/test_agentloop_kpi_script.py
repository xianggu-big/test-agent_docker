# -*- coding: utf-8 -*-
"""A0-2（2026-11-27）：agentloop_kpi 脚本两处口径错的红测试。

①workspace_of 源码收集只读 .js/.py，漏 .go/.ts/.php/.java——go-api-book 的
  S-K3 真实率被压到 0.657（按子串口径真值 1.0）；生成用例收集也漏
  TestGen*_test.go / TestGen*.php / src/test/java/TestGen*.java（落地率同被压）。
②S-K4 落地率分母设计是"真实的优先项"（n_concrete_real），写成了 n_concrete。
"""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KPI = HERE.parent / "scripts" / "agentloop_kpi.py"
sys.path.insert(0, str(HERE))


def _load():
    spec = importlib.util.spec_from_file_location("agentloop_kpi", KPI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_env(tmp_path, monkeypatch):
    """独立临时库 + agent_runs/agent_steps 各一行 strategy finish（带两条优先项：
    一条真实（books/deletebook 落在工作区）+ 一条具体但非真实（handling）。"""
    from test_agent import config
    from test_agent.store import db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    dbp = tmp_path / "t.db"
    c = db.get_conn(dbp)
    db.init_db(dbp)
    from test_agent.skills import run_ledger
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid
    c.commit()
    sid, rid = run_ledger.start_run(c, tid, kind="single", goal_text="g")
    finish = {"action": "finish", "summary": "s", "strategy": {"priorities": [
        {"order": 1, "capability_id": "test.gen.write",
         "why": "books.go 的 DeleteBook 未测，补 deletebook 断言",
         "args_hint": {"targets": ["src/books.go"], "feedback": "deletebook"}},
        {"order": 2, "capability_id": "test.gen.write",
         "why": "improve error handling", "args_hint": {}},
    ]}}
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=1,
                           action_type="finish", allowed=1,
                           decision_json=json.dumps(finish, ensure_ascii=False))
    run_ledger.finish_run(c, rid, status="done")
    # 合成工作区：go 源码 + 一份生成用例（含 deletebook）
    ws = tmp_path / "data" / "workspaces" / f"task_{tid}"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "books.go").write_text(
        "package main\n\n// books.go — deletebook endpoint handler\n"
        "func DeleteBook(id int) error { return nil }\n", encoding="utf-8")
    (ws / "test_gen_r1.py").write_text(
        "def test_deletebook():\n    assert deletebook(1) is None\n", encoding="utf-8")
    runs = [{"task_id": tid}]
    return kpi_mod(), c, runs, tid


def kpi_mod():
    return _load()


def test_workspace_of_reads_go_sources(tmp_path, monkeypatch):
    """源码符号集必须含 .go（同理 .ts/.php/.java）——旧版漏读 → S-K3 被压。"""
    mod, c, runs, tid = _make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    texts = mod.workspace_of(c, tid)
    assert any("DeleteBook" in s for s in texts), ".go 源码必须进符号集"
    assert any("deletebook endpoint" in s for s in texts)


def test_workspace_cases_read_go_case_files(tmp_path, monkeypatch):
    """cases_only=True 必须收 TestGen*_test.go 形态的生成用例（go 批真实文件名）。"""
    mod, c, runs, tid = _make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "data" / "workspaces" / f"task_{tid}" / "TestGenR2_test.go").write_text(
        "func TestDeleteBookMissing(t *testing.T) { deletebook(404) }\n", encoding="utf-8")
    landed = mod.workspace_of(c, tid, cases_only=True)
    assert any("deletebook(404)" in s for s in landed), "TestGen*_test.go 必须进用例集"


def test_sk4_denominator_is_real_not_concrete(tmp_path, monkeypatch):
    """S-K4 落地率分母=真实的优先项（n_concrete_real），不是 n_concrete——
    两条优先项（1 真实落地 + 1 具体非真实）下：旧口径 1/2，设计口径 1/1。"""
    mod, c, runs, tid = _make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    m = mod.strategy_metrics(c, runs, mod.workspace_of)
    assert m["n_priorities"] == 2
    assert m["S_K3_real_rate"] == 0.5              # 1 真实 / 2 具体
    assert m["S_K4_landed_rate"] == 1.0            # 1 落地 / 1 真实（设计口径）
