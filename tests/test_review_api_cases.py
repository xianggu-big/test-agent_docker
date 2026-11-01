"""任务A（2026-11-01 任务卡）后端契约单测：/cases?all=1 全量分组 + /feedback 附 cases 明细。
tmp 库；不启服务/不碰 Docker/不写真实库。"""
import pytest

from test_agent import config
from test_agent.api import server as srv
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "rv.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,status)"
        " VALUES(1,1,'需求',1,'ready_for_review')").lastrowid
    # 两个脚本：原始 r1（version=1，2 条）+ 增补 fb1（version=101，1 条）
    # ——复刻 #281 实况（反馈增补 version 更高，旧端点只回最新脚本把原始用例藏掉）
    s1 = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash)"
        " VALUES(?,'pytest',1,'test_gen_r1.test.js','c','h1')", (tid,)).lastrowid
    s2 = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash)"
        " VALUES(?,'jest',101,'test_gen_fb1.test.js','c','h2')", (tid,)).lastrowid
    for sid, name, exp in [(s1, "test_a", "加法返回和"), (s1, "test_b", "边界 clamp"),
                           (s2, "test_c", "NaN 输入防御")]:
        conn.execute(
            "INSERT INTO test_cases(script_id,source,name,method,expected,expected_basis,content)"
            " VALUES(?,'ai',?, '边界值',?, '需求明示','def t(): pass')", (sid, name, exp))
    conn.commit()
    conn.close()
    return {"tid": tid, "s1": s1, "s2": s2}


def test_cases_默认契约不变_只回最新脚本(tmp_db):
    out = srv.api_cases(tmp_db["tid"], False, {"user_id": 1, "role": "caller"})
    assert out["script"]["id"] == tmp_db["s2"]          # version DESC LIMIT 1 = fb1
    assert [c["name"] for c in out["cases"]] == ["test_c"]


def test_cases_all_分组返回全部脚本(tmp_db):
    out = srv.api_cases(tmp_db["tid"], True, {"user_id": 1, "role": "caller"})
    assert [s["filename"] for s in out["scripts"]] == \
        ["test_gen_r1.test.js", "test_gen_fb1.test.js"]   # 按 version 升序=时间序
    assert out["scripts"][0]["cases"][0]["expected"] == "加法返回和"
    flat_names = [c["name"] for c in out["cases"]]
    assert flat_names == ["test_a", "test_b", "test_c"]   # 扁平全量，原始用例不再被藏
    assert all(c["script_id"] and c["version"] is not None and "filename" in c
               for c in out["cases"])                     # A2 契约字段齐全


def test_cases_all_无脚本_空态(tmp_db):
    conn = get_conn()
    conn.execute("DELETE FROM test_cases")
    conn.execute("DELETE FROM test_scripts")
    conn.commit()
    conn.close()
    out = srv.api_cases(tmp_db["tid"], True, {"user_id": 1, "role": "caller"})
    assert out == {"scripts": [], "cases": []}


def test_feedback_摘要附新增用例明细(tmp_db, monkeypatch):
    """run_feedback_round 打桩（不调 LLM）——端点在 summary 基础上附 cases 数组。"""
    from test_agent.graph import builder
    monkeypatch.setattr(
        builder, "run_feedback_round",
        lambda tid, fb: {"generated": 1, "file": "test_gen_fb1.test.js",
                         "script_id": tmp_db["s2"], "static_rejected": []})
    out = srv.api_feedback(tmp_db["tid"], {"feedback": "补 NaN 用例"},
                           {"user_id": 1, "role": "developer"})
    assert out["generated"] == 1
    assert len(out["cases"]) == 1
    c = out["cases"][0]
    assert c["name"] == "test_c" and c["method"] == "边界值"
    assert c["expected"] == "NaN 输入防御" and c["expected_basis"] == "需求明示"
    assert out["filename"] == "test_gen_fb1.test.js"


def test_feedback_无script_id_空cases不炸(tmp_db, monkeypatch):
    from test_agent.graph import builder
    monkeypatch.setattr(
        builder, "run_feedback_round",
        lambda tid, fb: {"generated": 0, "reason": "闸门全剔（诚实降级）"})
    out = srv.api_feedback(tmp_db["tid"], {"feedback": "x"},
                           {"user_id": 1, "role": "developer"})
    assert out["cases"] == [] and out["generated"] == 0
