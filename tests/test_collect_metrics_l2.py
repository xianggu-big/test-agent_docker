"""collect_metrics 的 L2 按级指标包单测（蓝图 §3.4：L2=端点覆盖/契约违背检出）。"""
import json

import pytest

from test_agent import config
from test_agent.skills.evaluate import collect_metrics
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    old = config.DB_PATH
    config.DB_PATH = db
    yield db
    config.DB_PATH = old


def _mk_task(db) -> int:
    conn = get_conn(db)
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,?)",
                       (pid, uid)).lastrowid
    conn.commit()
    conn.close()
    return tid


def test_collect_metrics_includes_l2_when_leg_exists(tmp_db):
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    conn.execute("INSERT INTO test_reports(task_id, passed, failed) VALUES (?, 30, 0)", (tid,))
    conn.execute("INSERT INTO test_levels(task_id, level, strategy, status, verdict, summary)"
                 " VALUES (?, 'L2', 'full', 'failed', 'stop', ?)",
                 (tid, json.dumps({"passed": 11, "failed": 1, "contract_violations": 1,
                                   "endpoints_total": 4, "endpoints_hit": 3,
                                   "endpoints_hit": 3})))
    conn.commit()
    m = collect_metrics(conn, tid)
    assert m["l2"]["verdict"] == "stop"
    assert m["l2"]["contract_violations"] == 1
    assert m["l2"]["endpoints_hit"] == 3
    assert m["l2"]["endpoints_hit"] == 3        # TODO-07 并存：新旧两键同值


def test_collect_metrics_no_l2_key_for_legacy_tasks(tmp_db):
    tid = _mk_task(tmp_db)
    m = collect_metrics(get_conn(tmp_db), tid)      # 无报告行也应有 no_report 且无 l2 键
    assert "l2" not in m and m.get("no_report") is True


def test_trace_excludes_l2_rows(tmp_db):
    """G2 回归锁：L2 leg 的轮次行不得混入 L1 轨迹（趋势图/判定分布纯度）。"""
    tid = _mk_task(tmp_db)
    conn = get_conn(tmp_db)
    conn.execute("INSERT INTO test_reports(task_id, passed, failed) VALUES (?, 30, 0)", (tid,))
    conn.execute("INSERT INTO evaluation_rounds(task_id, level, round_no, passed, failed,"
                 " verdict) VALUES (?, 'L1', 1, 30, 0, 'pass')", (tid,))
    conn.execute("INSERT INTO evaluation_rounds(task_id, level, round_no, passed, failed,"
                 " verdict) VALUES (?, 'L2', 1, 11, 1, 'stop')", (tid,))
    conn.commit()
    m = collect_metrics(conn, tid)
    assert len(m["trace"]) == 1 and m["trace"][0]["verdict"] == "pass"
    assert m["verdict_counts"] == {"pass": 1}          # L2 的 stop 不进判定分布
