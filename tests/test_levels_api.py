"""D10 四级卡片后端 API 单测（tmp 库；不启服务/不碰 Docker）。"""
import json

import pytest

from test_agent import config
from test_agent.api.server import api_levels
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "levels.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed)"
        " VALUES(1,1,'需求',1)").lastrowid
    conn.commit()
    conn.close()
    return tid


def _seed(tid):
    conn = get_conn()
    conn.execute(
        "INSERT INTO test_levels(task_id,level,status,verdict,summary,trace_from)"
        " VALUES(?, 'L1','done','pass',?, NULL)", (tid, json.dumps({"passed": 9})))
    conn.execute(
        "INSERT INTO test_levels(task_id,level,status,verdict,summary)"
        " VALUES(?, 'L2','failed','stop',?)",
        (tid, json.dumps({"contract_violations": 1})))
    conn.execute(
        "INSERT INTO scenario_cases(task_id,level,title,steps,status)"
        " VALUES(?, 'L3','流1','[{\"method\":\"POST\",\"path\":\"/x\"}]','pending_confirm')",
        (tid,))
    conn.execute(
        "INSERT INTO scenario_cases(task_id,level,title,steps,status)"
        " VALUES(?, 'L4','验1','[{\"method\":\"POST\",\"path\":\"/x\"}]','confirmed_pass')",
        (tid,))
    conn.commit()
    conn.close()


def test_levels_按级排序与summary解析(tmp_db):
    _seed(tmp_db)
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert [l["level"] for l in out["levels"]] == ["L1", "L2"]
    assert out["levels"][0]["summary"] == {"passed": 9}
    assert out["levels"][1]["summary"] == {"contract_violations": 1}
    assert out["scenarios"]["L3"]["total"] == 1
    assert out["scenarios"]["L4"]["total"] == 1
    assert out["trace_count"] == 1               # pending_confirm 触发溯源
    assert out["trace_chains"][0]["trigger"]["status"] == "pending_confirm"


def test_levels_无leg如实缺席(tmp_db):
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert out["levels"] == []
    assert out["scenarios"] == {"L3": None, "L4": None}
    assert out["trace_count"] == 0


def test_levels_不存在任务404(tmp_db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api_levels(9999, {"user_id": 1, "role": "admin"})
    assert e.value.status_code == 404
