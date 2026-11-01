"""任务C（2026-11-01 任务卡）单测：junit/jest 逐用例解析、落库幂等/origin、
execution 快照与 API。tmp 库；不碰 Docker/LLM。"""
import json

import pytest

from test_agent import config
from test_agent.api import server as srv
from test_agent.skills.case_results import (
    execution_snapshot, parse_jest_results, parse_junit_xml, store_case_results)
from test_agent.store.db import get_conn, init_db

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite errors="1" failures="1" skipped="1" tests="4" time="0.05">
  <testcase classname="test_gen_r1" name="test_add" time="0.01"/>
  <testcase classname="test_gen_r1" name="test_clamp_high[edge-5]" time="0.02">
    <failure message="AssertionError: assert 10 == 5" type="AssertionError">traceback...</failure>
  </testcase>
  <testcase classname="test_gen_r1" name="test_broken" time="0.001">
    <error message="ImportError: no module named x" type="ImportError">traceback...</error>
  </testcase>
  <testcase classname="test_gen_r1" name="test_skip" time="0.0">
    <skipped message="deselected"/>
  </testcase>
</testsuite>"""


def test_parse_junit_四态与参数化剥除():
    rows = parse_junit_xml(JUNIT)
    assert [r["status"] for r in rows] == ["passed", "failed", "error", "skipped"]
    assert rows[1]["name"] == "test_clamp_high"          # [edge-5] 参数化后缀剥除
    assert rows[1]["nodeid"] == "test_gen_r1::test_clamp_high[edge-5]"
    assert rows[1]["duration_ms"] == 20
    assert "AssertionError" in rows[1]["message"]
    assert rows[2]["status"] == "error" and "ImportError" in rows[2]["message"]


def test_parse_junit_坏xml_诚实空():
    assert parse_junit_xml("not xml at all") == []
    assert parse_junit_xml("") == []


def test_parse_jest_逐用例明细():
    jest = json.dumps({"numPassedTests": 1, "numFailedTests": 1, "testResults": [{
        "assertionResults": [
            {"fullName": "calc add returns sum", "status": "passed", "duration": 5},
            {"fullName": "calc clamp high", "status": "failed", "duration": 3,
             "failureMessages": ["Expected: 5 Received: 10"]}],
    }]})
    rows = parse_jest_results(jest)
    assert [r["status"] for r in rows] == ["passed", "failed"]
    assert rows[1]["name"] == "high"                     # fullName 末词=测试标题（jest 语义）
    assert "Expected: 5" in rows[1]["message"]


def test_parse_jest_非json_诚实空():
    assert parse_jest_results("jest crashed\n") == []


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,confirmed,status)"
        " VALUES(1,1,1,'running')").lastrowid
    sid = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash)"
        " VALUES(?,'pytest',1,'test_gen_r1.py','c','h')", (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id,source,name,content)"
                 " VALUES(?,'ai','test_add','def test_add(): pass')", (sid,))
    conn.commit()
    conn.close()
    return tid


def test_store_落库与origin分类_轮次delete_first(tmp_db):
    conn = get_conn()
    rows = parse_junit_xml(JUNIT)
    n1 = store_case_results(conn, tmp_db, 1, rows)
    assert n1 == 4
    got = conn.execute(
        "SELECT name, origin, status FROM test_case_results WHERE task_id=? ORDER BY id",
        (tmp_db,)).fetchall()
    origins = {r["name"]: r["origin"] for r in got}
    assert origins["test_add"] == "generated"           # 在 test_cases 里 → generated
    assert origins["test_broken"] == "repo"              # 不在 → 自带/未登记
    # 第 2 轮重存：round=1 的旧行不被清（round_no>1 不 delete-first），追加
    store_case_results(conn, tmp_db, 2, [rows[0]])
    n_rounds = conn.execute(
        "SELECT COUNT(*) AS n FROM test_case_results WHERE task_id=?", (tmp_db,)).fetchone()["n"]
    assert n_rounds == 5
    # 但重新从第 1 轮跑（重跑任务）：delete-first 以本轮为准
    store_case_results(conn, tmp_db, 1, [rows[0]])
    n_after = conn.execute(
        "SELECT COUNT(*) AS n FROM test_case_results WHERE task_id=?", (tmp_db,)).fetchone()["n"]
    assert n_after == 1
    conn.close()


def test_execution_snapshot_组装与空态(tmp_db):
    conn = get_conn()
    store_case_results(conn, tmp_db, 1, parse_junit_xml(JUNIT))
    conn.execute(
        "INSERT INTO evaluation_rounds(task_id,round_no,passed,failed,skipped,coverage_line,verdict)"
        " VALUES(?,1,2,1,1,0.5,'pass')", (tmp_db,))
    conn.execute(
        "INSERT INTO agent_sessions(task_id) VALUES(?)", (tmp_db,))
    sid = conn.execute("SELECT id FROM agent_sessions WHERE task_id=?", (tmp_db,)).fetchone()["id"]
    conn.execute("INSERT INTO agent_messages(session_id,seq,role,content) VALUES(?, 1, 'assistant', '[run 第1轮] x')",
                 (sid,))
    conn.commit()
    snap = execution_snapshot(conn, tmp_db)
    assert snap["status"] == "running"
    assert len(snap["cases"]) == 4 and snap["granularity"] == "round"
    assert snap["rounds"][0]["passed"] == 2
    assert any("[run" in ln for ln in snap["log_tail"])
    # API 层：404 与正常路径
    assert "error" not in srv.api_execution(tmp_db, {"user_id": 1, "role": "caller"})
    conn.close()


def test_execution_api_不存在任务404(tmp_path):
    import sqlite3
    with pytest.raises(Exception):
        srv.api_execution(99999, {"user_id": 1, "role": "caller"})


def test_受控集成_真pytest输出端到端(tmp_path, monkeypatch):
    """C3 受控测试（不碰 Docker/LLM）：宿主真跑 3 条用例（过/败/参数化），
    真实 junitxml → 解析 → 落库 → 快照——验证解析器吃的是真 pytest 输出而非手写fixture。"""
    import subprocess
    import sys
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_demo.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n"
        "def test_fail():\n    assert 2 + 2 == 5\n"
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_param(x):\n    assert x > 0\n", encoding="utf-8")
    junit_file = tmp_path / "junit.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml", str(junit_file),
                    str(ws / "test_demo.py")], check=False, capture_output=True, cwd=str(ws))
    rows = parse_junit_xml(junit_file.read_text(encoding="utf-8"))
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["failed", "passed", "passed", "passed"]      # 4=参数化展开 2 条
    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r["status"])
    assert by_name["test_fail"] == ["failed"]
    assert "AssertionError" in next(r["message"] for r in rows if r["status"] == "failed")
    # 落库+快照（tmp 库复用 fixture 语义，独立建避免依赖顺序）
    from test_agent.store.db import init_db as _init
    db = tmp_path / "e2e.db"
    _init(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,confirmed) VALUES(1,1,1)").lastrowid
    conn.commit()
    assert store_case_results(conn, tid, 1, rows) == 4
    snap = execution_snapshot(conn, tid)
    assert len(snap["cases"]) == 4 and snap["granularity"] == "round"
    conn.close()
