"""报告概览·判决先行配套：缺陷分析证据包（确定性）/ 解析 fail-closed / 端点守卫。tmp 库。"""
import json
from types import SimpleNamespace

import pytest

from test_agent import config
from test_agent.graph import repair
from test_agent.skills import defect_analysis as da
from test_agent.store import db


def _seed(conn):
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name, primary_language) VALUES (?, 'r', 'n', 'python')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed, status) "
                       "VALUES (1, ?, '需求：clamp 闭区间', 1, 'failed')", (uid,)).lastrowid
    sid = conn.execute("INSERT INTO test_scripts(task_id, framework, version, filename, content, content_hash) "
                       "VALUES (?, 'pytest', 1, 'test_gen_r1.py', 'x', 'h')", (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content, expected, input, expected_basis) "
                 "VALUES (?, 'ai', 'test_clamp_hi', '边界值分析', 'def test_clamp_hi(): ...', '返回 10', 'clamp(10,0,10)', '需求明示')", (sid,))
    conn.execute("INSERT INTO test_reports(task_id, passed, failed, skipped, coverage_line, defects) "
                 "VALUES (?, 5, 1, 0, 1.0, ?)", (tid, json.dumps({"failure_kind": "product_defect", "stop_reason": "疑似产品缺陷"})))
    conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')", (tid,))
    ses = conn.execute("SELECT id FROM agent_sessions WHERE task_id=?", (tid,)).fetchone()["id"]
    conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?, 0, 'assistant', ?)",
                 (ses, "[evaluate·failed] FAILED test_gen_r1.py::test_clamp_hi - AssertionError: assert 9 == 10"))
    conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?, 1, 'assistant', ?)",
                 (ses, "[evaluate·failed] FAILED tests/test_old.py::test_repo - AssertionError: x"))
    conn.commit()
    return tid


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    tid = _seed(conn)
    yield conn, tid
    conn.close()


def test_failure_summary_of():
    out = "FAILED a.py::test_x - AssertionError: assert 1 == 2\nFAILED a.py::test_y - TypeError: bad"
    assert repair.failure_summary_of(out, "a.py::test_x") == "AssertionError: assert 1 == 2"
    assert repair.failure_summary_of(out, "a.py::test_zzz") == ""


def test_collect_failure_evidence(env):
    conn, tid = env
    ev = da.collect_failure_evidence(conn, tid)
    assert ev["failure_kind"] == "product_defect" and ev["requirement"].startswith("需求")
    assert len(ev["failed"]) == 2
    gen = next(f for f in ev["failed"] if f["name"] == "test_clamp_hi")
    assert gen["error_class"] == "AssertionError" and gen["expected"] == "返回 10"
    assert gen["expected_basis"] == "需求明示" and gen["origin"] == "generated"
    repo = next(f for f in ev["failed"] if f["name"] == "test_repo")
    assert repo["origin"] == "repo" and repo["expected"] is None          # 自带测试无六要素，如实为空


def test_parse_analysis_fail_closed():
    ok = da.parse_analysis('{"summary":"集中在 clamp 上界","defects":[{"case":"test_clamp_hi","fix":"把 >= 改成 >","confidence":"高"}]}')
    assert ok["summary"] and ok["defects"][0]["fix"].startswith("把")
    assert da.parse_analysis('{"defects":[{"case":"x"}]}') is None        # 无 fix 且无 summary → None
    assert da.parse_analysis("非 JSON") is None


def test_run_defect_analysis_caches(env, monkeypatch):
    conn, tid = env

    class FakeLLM:
        def invoke(self, messages):
            user = messages[1][1]
            assert "assert 9 == 10" in user and "返回 10" in user          # 证据真的进了提示词
            return SimpleNamespace(content=json.dumps({"summary": "上界 >= 应为 >", "defects": [
                {"case": "test_clamp_hi", "observed": "返回 9", "expected": "返回 10",
                 "root_cause": "clamp 用 x >= hi 返回 hi-1", "fix": "改为 x > hi", "confidence": "高"}]},
                ensure_ascii=False), usage_metadata=None)
    monkeypatch.setattr(da, "ChatOpenAI", lambda **kw: FakeLLM())
    out = da.run_defect_analysis("k", conn, tid, ws=None)
    assert out["defects"][0]["fix"] == "改为 x > hi" and out["evidence_count"] == 2
    assert "推测" in out["note"]
    cached = conn.execute("SELECT analysis FROM test_reports WHERE task_id=?", (tid,)).fetchone()["analysis"]
    assert json.loads(cached)["summary"] == "上界 >= 应为 >"


def test_run_defect_analysis_no_failures_skips(env):
    conn, tid = env
    conn.execute("DELETE FROM agent_messages")
    conn.commit()
    out = da.run_defect_analysis("k", conn, tid)
    assert out["skipped"]


def test_analysis_endpoints(env, monkeypatch):
    conn, tid = env
    from test_agent.api import server
    import fastapi
    got = server.api_get_analysis(tid, {"user_id": 1, "role": "admin"})
    assert got["analysis"] is None and len(got["evidence"]["failed"]) == 2
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(fastapi.HTTPException) as ei:
        server.api_run_analysis(tid, {"user_id": 1, "role": "admin"})
    assert ei.value.status_code == 409                                   # 无 Key 诚实拒绝
