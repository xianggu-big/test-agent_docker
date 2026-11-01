"""任务B（2026-11-01）单测：RTM 端点无映射快照时用已落库条件构建「未映射」行
（不伪造 rtm_maps；有快照时行为逐字节不变）。tmp 库。"""
import pytest

from test_agent import config
from test_agent.api import server as srv
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "rtm.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed)"
        " VALUES(1,1,'需求',1)").lastrowid
    for cond in ("调用 add 返回算术和", "div(0) 抛异常"):
        conn.execute("INSERT INTO test_conditions(task_id,condition,priority) VALUES(?,?,'高')",
                     (tid, cond))
    conn.commit()
    conn.close()
    return tid


def test_无快照_条件构建未映射行(tmp_db):
    out = srv.api_rtm(tmp_db, {"user_id": 1, "role": "caller"})
    assert out["conditions_total"] == 2
    assert out["map_source"] is None                      # 未伪造快照来源
    assert [m["status"] for m in out["matrix"]] == ["unmapped", "unmapped"]
    assert out["matrix"][0]["condition"] == "调用 add 返回算术和"
    assert out["matrix"][0]["cases"] == []                # 没跑评估就没有用例映射，不冒充


def test_有快照_行为不变(tmp_db):
    import json
    conn = get_conn()
    conn.execute(
        "INSERT INTO rtm_maps(task_id, matrix, source) VALUES(?,?, 'heuristic')",
        (tmp_db, json.dumps([{"condition": "调用 add 返回算术和", "priority": "高",
                              "cases": ["test_add"], "status": "covered"}])))
    conn.commit()
    conn.close()
    out = srv.api_rtm(tmp_db, {"user_id": 1, "role": "caller"})
    assert out["map_source"] == "heuristic"
    assert out["matrix"][0]["status"] == "covered"        # 快照优先，未映射构建不掺和


def test_无条件无快照_全空(tmp_db):
    conn = get_conn()
    conn.execute("DELETE FROM test_conditions")
    conn.commit()
    conn.close()
    out = srv.api_rtm(tmp_db, {"user_id": 1, "role": "caller"})
    assert out["matrix"] == [] and out["conditions_total"] == 0
