"""时间戳口径锁（2026-11-15 闲时找bug 第一轮，R5 数据正确性维度）。

全库时间戳口径 = SQLite datetime('now')（UTC 无时区）。两处偏离写本地时间：
- graph/l2.py _now()：test_levels.started_at/finished_at（L1~L4 leg 全用它）——
  与同链路 agent_sessions.ended_at（UTC）并排展示，UTC+8 下级别卡时间超前 8 小时；
- skills/scenario_gen.confirm_scenario 的 confirmed_at——与同表 created_at
  （DEFAULT datetime('now')）相差时区偏移，按时间排序/对比错位。

锁法：解析产物时间与 datetime.now(timezone.utc) 差 <120s（本地时区机器上旧代码
差 8 小时必红；UTC 机器上锁住不回归）。
"""
from datetime import datetime, timezone


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def test_l2_now_is_utc():
    from test_agent.graph.l2 import _now
    ts = _parse(_now())
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((ts - utc).total_seconds()) < 120, "_now 必须是 UTC（与 datetime('now') 同口径）"


def test_confirm_scenario_writes_utc_confirmed_at(tmp_path, monkeypatch):
    from test_agent import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    from test_agent.skills.scenario_gen import confirm_scenario
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed) VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid
    sid = c.execute(
        "INSERT INTO scenario_cases(task_id, level, title, steps, status) "
        "VALUES(?, 'L3', '重复删除语义', '[]', 'pending_confirm')", (tid,)).lastrowid
    c.commit()
    out = confirm_scenario(c, sid, "pass", "", uid)
    assert out["status"] == "confirmed_pass"
    row = c.execute("SELECT confirmed_at, created_at FROM scenario_cases WHERE id=?",
                    (sid,)).fetchone()
    c.close()
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((_parse(row["confirmed_at"]) - utc).total_seconds()) < 120, \
        "confirmed_at 必须是 UTC（与同表 created_at=datetime('now') 同口径）"
