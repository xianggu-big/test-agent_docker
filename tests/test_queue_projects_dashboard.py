"""B1/B2/B3（2026-11-02 方案一）单测：项目归一 / 执行队列 / 分页与指挥台聚合。tmp 库。"""
import json

import pytest

from test_agent import config
from test_agent.api import server as srv
from test_agent.store.db import get_conn, init_db
from test_agent.store.projects import (backfill_project_keys, ensure_project,
                                       normalize_repo_key, project_display_name)
from test_agent.store.queue import QueueWorker


# ---------- B2 项目归一 ----------

def test_normalize_local_path_forms_merge():
    a = normalize_repo_key(r"D:\proj\demo")
    b = normalize_repo_key(r"d:/proj/demo/")
    c = normalize_repo_key(r"D:\proj\demo\.\ ")
    assert a == b == c
    assert a.startswith("local:")


def test_normalize_url_forms_merge():
    a = normalize_repo_key("https://GitHub.com/Owner/Repo.git")
    b = normalize_repo_key("https://github.com/owner/Repo/")
    assert a == b == "url:https://github.com/owner/repo"   # 已知 host path 一并小写
    assert normalize_repo_key("git@github.com:o/r.git") == "url:git@github.com:o/r"
    # 自建服务保留 path 大小写（可能敏感）
    assert normalize_repo_key("https://git.mycorp.com/Team/Repo") ==         "url:https://git.mycorp.com/Team/Repo"


def test_display_name():
    assert project_display_name("https://github.com/o/my-repo.git") == "my-repo"
    assert project_display_name(r"D:\x\y\zzz") == "zzz"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    init_db(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = get_conn(db)
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.commit()
    conn.close()
    return db


def test_ensure_project_reuses_same_key(tmp_db):
    conn = get_conn()
    pid1, n1, created1 = ensure_project(conn, 1, r"D:\proj\alpha")
    pid2, n2, created2 = ensure_project(conn, 1, r"D:/proj/alpha/")
    assert pid1 == pid2 and created1 is True and created2 is False
    assert n1 == n2 == "alpha"
    assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1
    conn.close()


def test_backfill_merges_duplicates_and_modules(tmp_db):
    conn = get_conn()
    # 造 3 条同 key 项目（模拟历史重复），其中两条各有同 path 模块（UNIQUE 冲突场景）
    p1 = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/x/demo','demo')").lastrowid
    p2 = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/x/demo/','demo')").lastrowid
    p3 = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/x/demo','demo')").lastrowid
    m1 = conn.execute("INSERT INTO modules(project_id,path,language) VALUES(?,'.','python')", (p1,)).lastrowid
    m2 = conn.execute("INSERT INTO modules(project_id,path,language) VALUES(?,'.','python')", (p2,)).lastrowid
    script = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,module_id,content,content_hash)"
        " VALUES(?, 'pytest', 1, ?, 'c', 'h')", (1, m2)).lastrowid if False else None
    t1 = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text) VALUES(?,1,'r1')", (p1,)).lastrowid
    t2 = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text) VALUES(?,1,'r2')", (p2,)).lastrowid
    conn.commit()

    dry = backfill_project_keys(conn, dry_run=True)
    assert dry["merged"] == 2 and dry["modules_moved"] == 1  # p2 的模块计入待搬
    assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 3   # dry 不改

    out = backfill_project_keys(conn, dry_run=False)
    assert out["merged"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1
    assert conn.execute("SELECT project_id FROM test_tasks WHERE id=?", (t2,)).fetchone()[0] == p1
    # 同 path 模块合并后只剩一个
    assert conn.execute("SELECT COUNT(*) AS n FROM modules").fetchone()["n"] == 1
    assert conn.execute("SELECT repo_key FROM projects WHERE id=?", (p1,)).fetchone()[0].startswith("local:")
    conn.close()


# ---------- B1 执行队列 ----------

def test_enqueue_idempotent(tmp_db):
    conn = get_conn()
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/q/q','q')")
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(1,1)").lastrowid
    conn.commit(); conn.close()
    q1 = QueueWorker.enqueue(tid, "exec")
    q2 = QueueWorker.enqueue(tid, "exec")
    assert q1 == q2                                   # 同任务同 kind 不重复排队
    assert QueueWorker.enqueue(tid, "prep") != q1       # kind 不同=不同工作
    assert QueueWorker.pending_count() == 2


def test_queue_recover_marks_running_interrupted(tmp_db):
    conn = get_conn()
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/q/r','r')")
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(1,1)").lastrowid
    qid = conn.execute("INSERT INTO exec_queue(task_id,kind,status) VALUES(?,'exec','running')",
                       (tid,)).lastrowid
    conn.commit()
    conn.close()
    QueueWorker()._recover()
    row = get_conn().execute("SELECT status, error FROM exec_queue WHERE id=?", (qid,)).fetchone()
    assert row["status"] == "failed" and "重启" in row["error"]


def test_queue_snapshot_shape(tmp_db):
    conn = get_conn()
    conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'D:/q/s','s')")
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(1,1)").lastrowid
    conn.commit(); conn.close()
    QueueWorker.enqueue(tid, "prep")
    snap = QueueWorker.queue_snapshot(5)
    assert snap and snap[0]["task_id"] == tid and snap[0]["kind"] == "prep"
    assert snap[0]["status"] == "queued"


# ---------- B3 分页 + 指挥台 ----------

def _seed_tasks(conn, n=12):
    proj = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language)"
                        " VALUES(1,'D:/p/d','demo','python')").lastrowid
    ids = []
    for i in range(n):
        st = "done" if i % 3 == 0 else ("failed" if i % 3 == 1 else "ready_for_review")
        ids.append(conn.execute(
            "INSERT INTO test_tasks(project_id,user_id,requirement_text,status)"
            " VALUES(?,1,?,?)", (proj, f"需求{i}", st)).lastrowid)
    conn.commit()
    return proj, ids


def test_tasks_page_pagination_and_filters(tmp_db):
    conn = get_conn(); _seed_tasks(conn, 12); conn.close()
    d = srv.api_tasks_page(limit=5, offset=0, status="", q="", user={"user_id": 1, "role": "admin"})
    assert d["total"] == 12 and len(d["items"]) == 5
    assert d["items"][0]["id"] > d["items"][-1]["id"]        # 倒序
    d2 = srv.api_tasks_page(limit=5, offset=10, status="", q="", user={"user_id": 1, "role": "admin"})
    assert len(d2["items"]) == 2
    d3 = srv.api_tasks_page(limit=50, offset=0, status="done,failed",
                            q="", user={"user_id": 1, "role": "admin"})
    assert d3["total"] == 8 and all(t["status"] in ("done", "failed") for t in d3["items"])
    d4 = srv.api_tasks_page(limit=50, offset=0, status="", q="需求1",
                            user={"user_id": 1, "role": "admin"})
    assert d4["total"] >= 3
    assert d["counts"].get("done") == 4                     # 各状态计数不受分页影响


def test_dashboard_aggregates(tmp_db):
    conn = get_conn()
    proj, ids = _seed_tasks(conn, 9)
    conn.execute("INSERT INTO evaluation_rounds(task_id,round_no,passed,failed,verdict)"
                 " VALUES(?,1,8,2,'pass')", (ids[0],))
    conn.execute("INSERT INTO test_reports(task_id,passed,failed,defects) VALUES(?,8,2,?)",
                 (ids[0], json.dumps({"failure_kind": "product_defect"})))
    conn.execute("INSERT INTO llm_calls(task_id,phase,prompt_tokens,completion_tokens,total_tokens) VALUES(?, 'plan', 80, 43, 123)",
                 (ids[0],))
    conn.commit(); conn.close()
    d = srv.api_dashboard(user={"user_id": 1, "role": "admin"})
    assert d["kpi"]["total"] == 9
    assert d["kpi"]["review"] == 3                           # 9 条里 3 条 ready_for_review
    assert d["kpi"]["defects"] >= 1
    assert d["kpi"]["pass_rate"] == pytest.approx(0.8, abs=0.01)
    assert len(d["pending_review"]) == 3
    assert d["projects"] and d["projects"][0]["n"] == 9
    assert d["feed"]
