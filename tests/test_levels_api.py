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


# ---------- D6：直接 L1 任务（未走 levels 流）的合成回退卡 ----------

def test_levels_普通L1任务合成回退卡(tmp_db):
    """有 evaluation_rounds 轨迹但无 test_levels 行的老式任务：合成 L1 卡进四级视图。"""
    conn = get_conn()
    conn.execute(
        """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,
               coverage_line, verdict, stop_reason, level)
           VALUES(?, 1, 28, 6, 0, 0.48, 'stop', '疑似产品缺陷', 'L1')""", (tmp_db,))
    conn.commit()
    conn.close()
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert len(out["levels"]) == 1
    card = out["levels"][0]
    assert card["level"] == "L1" and card["synthetic"] is True
    assert card["summary"]["passed"] == 28 and card["summary"]["failed"] == 6
    assert card["summary"]["coverage_line"] == 0.48
    assert card["status"] == "queued"  # 裸任务初始态如实透传


def test_levels_仅有用例无轮次也出卡(tmp_db):
    """只有 test_cases 没有 rounds 的任务：cases_total 兜底，verdict 如实缺席。"""
    conn = get_conn()
    sid = conn.execute(
        "INSERT INTO test_scripts(task_id, framework, version, content, content_hash)"
        " VALUES(?, 'pytest', 1, 'x', 'h')", (tmp_db,)).lastrowid
    conn.execute(
        "INSERT INTO test_cases(script_id, source, name, content) VALUES(?, 'ai', 'test_a', 'x')",
        (sid,))
    conn.commit()
    conn.close()
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert len(out["levels"]) == 1
    card = out["levels"][0]
    assert card["synthetic"] is True
    assert card["summary"]["cases_total"] == 1
    assert card["verdict"] is None and card["summary"]["passed"] is None


def test_levels_空任务仍如实无卡(tmp_db):
    """既无 rounds 也无用例：不出卡（诚实缺席，不造假数据）。"""
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert out["levels"] == []


def test_levels_合成卡只取L1轮次(tmp_db):
    """P2-4（体检）：混合轮次任务合成卡不得拿 L2 轮次冒充 L1（旧行为 ORDER BY 轮次可取到 L2）。"""
    conn = get_conn()
    for round_no, passed, level in ((1, 21, "L1"), (1, 99, "L2")):
        conn.execute(
            """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,
                   coverage_line, verdict, level) VALUES(?,?,?,?,?,?,?,'X')""",
            (tmp_db, round_no, passed, 0, 0, 0.5, "pass"))
        conn.execute("UPDATE evaluation_rounds SET level=? WHERE task_id=? AND round_no=? AND passed=?",
                     (level, tmp_db, round_no, passed))
    conn.commit()
    conn.close()
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    card = out["levels"][0]
    assert card["synthetic"] is True
    assert card["summary"]["passed"] == 21  # 只认 level='L1' 的轮次，L2 的 99 不冒充


def test_levels_仅L2轮次无L1信号不出卡(tmp_db):
    """只有 L2 轮次（无 L1 轮次/无用例）：不出合成卡（诚实缺席）。"""
    conn = get_conn()
    conn.execute(
        """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,
               coverage_line, verdict, level) VALUES(?, 1, 99, 1, 0, 0.5, 'pass', 'L2')""",
        (tmp_db,))
    conn.commit()
    conn.close()
    out = api_levels(tmp_db, {"user_id": 1, "role": "admin"})
    assert out["levels"] == []
