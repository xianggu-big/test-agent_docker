"""任务列表按项目分组：/api/tasks 新增 cases/coverage 按任务字段（additive）+ 分组数据充足性。"""
import pytest

from test_agent import config
from test_agent.api.server import api_tasks
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language)"
                       " VALUES(?,?,?,?)", (uid, "r", "coupon-api", "Python")).lastrowid
    pid2 = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language)"
                        " VALUES(?,?,?,?)", (uid, "r2", "tenacity", "Python")).lastrowid
    t1 = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,status)"
                      " VALUES(?,?,?,1,'done')", (pid, uid, "优惠券功能测试")).lastrowid
    t2 = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,status)"
                      " VALUES(?,?,?,1,'done')", (pid, uid, "L2 契约接力")).lastrowid
    t3 = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,status)"
                      " VALUES(?,?,?,1,'done')", (pid2, uid, "tenacity 重试语义")).lastrowid
    for t in (t1, t2, t3):
        conn.execute("INSERT INTO test_reports(task_id,passed,failed,coverage_line)"
                     " VALUES (?,30,0,0.95)", (t,))
    sc = conn.execute("INSERT INTO test_scripts(task_id,framework,version,content,content_hash)"
                      " VALUES (?, 'pytest',1,'x','h')", (t1,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id,source,name,content) VALUES"
                 f" ({sc},'ai','t1','x')"), 
    conn.execute("INSERT INTO test_cases(script_id,source,name,content) VALUES"
                 f" ({sc},'ai','t2','x')")
    conn.commit()
    conn.close()
    return {"t1": t1, "t2": t2, "t3": t3, "pid": pid, "pid2": pid2}


def test_api_tasks_has_cases_and_coverage(tmp_db):
    rows = api_tasks(user={"role": "dev", "user_id": 1})
    by_id = {r["id"]: r for r in rows}
    assert by_id[tmp_db["t1"]]["cases"] == 2          # t1 有 2 条用例
    assert by_id[tmp_db["t2"]]["cases"] == 0          # t2 无脚本 → 0（不是 NULL）
    assert by_id[tmp_db["t1"]]["coverage"] == 0.95
    assert all(r["project_name"] in ("coupon-api", "tenacity") for r in rows)


def test_api_tasks_groupable_by_project(tmp_db):
    rows = api_tasks(user={"role": "dev", "user_id": 1})
    groups = {}
    for r in rows:
        groups.setdefault(r["project_name"], []).append(r)
    assert len(groups["coupon-api"]) == 2             # 同项目两条运行聚在一组
    assert len(groups["tenacity"]) == 1
    assert groups["coupon-api"][0]["primary_language"] == "Python"
