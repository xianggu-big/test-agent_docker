"""Dashboard KPI 口径锁（2026-11-15 闲时找bug 第一轮，R5 数据正确性维度）。

/api/dashboard 三处聚合与自身注释/报告判决卡口径错位：
1. 通过率分母剔除 skipped——报告判决卡（server /report 与 skills/evaluate）分母
   含 skipped（passed+failed+skipped）。同一任务两处通过率不同，skipped 越多差越大；
2. defects KPI 按 test_reports 行计数——report 节点每跑一次 INSERT 一行，重跑翻倍；
   应按任务去重（与旁边任务总数 KPI 同口径）；
3. "最近 30 个有评估轮次的任务"按 task_id 倒序——老任务重跑后有最新轮次也挤不进
   前 30；应按最新轮次（MAX(e.id)）取近。
"""
import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)   # 防真 worker 干扰
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        yield client, h, db.get_conn()


def _seed_task(c, i):
    uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    return c.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
        "VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid


def test_pass_rate_matches_report_card_denominator(env):
    """报告判决卡：8 过 0 败 2 跳 = 80%。dashboard 必须同口径（旧=剔除 skipped 得 100%）。"""
    client, h, c = env
    tid = _seed_task(c, 1)
    c.execute("INSERT INTO evaluation_rounds(task_id,level,round_no,passed,failed,skipped,"
              "verdict) VALUES(?, 'L1', 1, 8, 0, 2, 'stop')", (tid,))
    c.commit()
    r = client.get("/api/dashboard", headers=h).json()["kpi"]
    assert r["pass_rate"] == pytest.approx(0.8), "分母必须含 skipped（判决卡口径）"


def test_defects_kpi_counts_distinct_tasks(env):
    """同一疑似缺陷任务重跑两次=两行 test_reports，KPI 仍应记 1 个缺陷任务。"""
    client, h, c = env
    tid = _seed_task(c, 1)
    c.execute("INSERT INTO test_reports(task_id,module_id,passed,failed,skipped,defects) "
              "VALUES(?, NULL, 8, 4, 0, '[\"product_defect\"]')", (tid,))
    c.execute("INSERT INTO test_reports(task_id,module_id,passed,failed,skipped,defects) "
              "VALUES(?, NULL, 8, 4, 0, '[\"product_defect\"]')", (tid,))
    c.commit()
    r = client.get("/api/dashboard", headers=h).json()["kpi"]
    assert r["defects"] == 1, "按报告行计数=重跑翻倍（应按任务去重）"


def test_recent_30_ordered_by_latest_evaluation(env):
    """31 个有轮次的任务，最老任务刚重跑出新轮次（rate=0）→ 应进入近 30 均值
    （旧按 task_id 倒序把 id 最小的它永远排除）。"""
    client, h, c = env
    for i in range(31):
        tid = _seed_task(c, i)
        c.execute("INSERT INTO evaluation_rounds(task_id,level,round_no,passed,failed,"
                  "skipped,verdict) VALUES(?, 'L1', 1, 1, 0, 0, 'stop')", (tid,))
    c.commit()
    # 任务 1（最早建）重跑出最新轮次：0 过 1 败
    first = c.execute("SELECT MIN(id) AS m FROM test_tasks").fetchone()["m"]
    c.execute("INSERT INTO evaluation_rounds(task_id,level,round_no,passed,failed,"
              "skipped,verdict) VALUES(?, 'L1', 2, 0, 1, 0, 'stop')", (first,))
    c.commit()
    r = client.get("/api/dashboard", headers=h).json()["kpi"]
    # 旧口径（task_id DESC LIMIT 30，排除 first）AVG=1.0；新口径含 first：
    # 其任务内两轮均值 (1.0+0.0)/2=0.5 → (0.5+29*1)/30=0.9833（"按任务均值"语义不变，
    # 只修"哪些任务算最近"的选取键）
    assert r["pass_rate"] == pytest.approx(29.5 / 30, abs=1e-3), \
        "近 30 必须按最新评估轮次取，不是按 task_id"
