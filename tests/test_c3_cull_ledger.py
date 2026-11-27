# -*- coding: utf-8 -*-
"""C3 剔除台账与队列收尾的回归测试（2026-11-27 午后，阶段 0 四次服务卡死的根根）。

事故链：旧 C3 把剔除项写进 test_cases（source='ai_cull'，违反该列 CHECK）→ 插入炸在
"用例已写入、尚未提交"之间 → 事务半开、写锁不释放 → 整库 "database is locked" →
队列收尾 UPDATE 也炸 → worker 被打回空闲轮询 → 队列行永远停在 running、任务晾在
"生成中"、服务对所有请求无响应。
本文件锁四件事：①台账写独立表且不碰 test_cases；②台账失败绝不抛出；
③gen_tests 不再出现白名单外的 source；④队列收尾写入失败不把 worker 打崩。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _env(tmp_path, monkeypatch):
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "test_agent.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'generating',1)", (pid, uid)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                    "content,content_hash) VALUES(?,'pytest',1,'test_gen_r1.py','c','h')",
                    (tid,)).lastrowid
    c.commit()
    return c, tid, sid, dbp


def test_log_culls_writes_own_table_not_test_cases(tmp_path, monkeypatch):
    from test_agent.skills.gen_tests import _log_culls
    c, tid, sid, _dbp = _env(tmp_path, monkeypatch)
    rows = [("test_a", "静态闸/断言质量闸剔除"), ("test_b", "模板展开失败")]
    n = _log_culls(c, tid, sid, "test_gen_r1.py", 1, rows)
    assert n == 2
    got = c.execute("SELECT name, reason, task_id, script_id FROM test_case_culls "
                    "ORDER BY id").fetchall()
    assert [(r["name"], r["reason"]) for r in got] == rows
    assert all(r["task_id"] == tid and r["script_id"] == sid for r in got)
    assert c.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == 0, \
        "剔除项不得写进 test_cases（旧实现的 CHECK 违反会锁死整库）"
    assert not c.in_transaction, "台账写完后不得留半开事务"


def test_log_culls_empty_and_failure_are_silent(tmp_path, monkeypatch):
    from test_agent.skills.gen_tests import _log_culls
    c, tid, sid, _dbp = _env(tmp_path, monkeypatch)

    class _Bomb:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            raise sqlite3.OperationalError("database is locked")

    assert _log_culls(c, tid, sid, "f", 1, []) == 0            # 空清单零写入
    assert _log_culls(_Bomb(), tid, sid, "f", 1, [("x", "y")]) == 0   # 失败只回 0，不抛


def test_gen_tests_has_no_non_whitelisted_source():
    """静态守卫：gen_tests 不得再向 test_cases 写白名单（builtin/ai/imported）之外的 source。"""
    src = (ROOT / "test_agent" / "skills" / "gen_tests.py").read_text(encoding="utf-8")
    helper = src.split("def _log_culls", 1)[1].split("def _parse_cases", 1)[0]
    assert "INSERT INTO test_case_culls" in helper, "台账必须写独立表"
    assert "INSERT INTO test_cases" not in helper, "台账不得碰 test_cases"
    # 主写入段（script_id 取得之后、台账调用之前）
    seg = src.split("script_id = cur.lastrowid", 1)[1].split("_log_culls(conn", 1)[0]
    assert '"ai",' in seg and "ai_cull" not in seg, "主写入段的 source 只能是 ai"
    assert "conn.rollback()" in seg, "主写入段失败必须回滚（防半开事务锁库）"


def test_step_survives_finish_write_failure(tmp_path, monkeypatch):
    """队列收尾 UPDATE 失败（整库被锁）时，_step 不得抛出——否则 worker 被打回空闲，
    队列行永远停在 running。"""
    from test_agent import config  # noqa: F401 —— 被 _env 打桩
    from test_agent.store import queue
    c, tid, _sid, dbp = _env(tmp_path, monkeypatch)
    c.execute("INSERT INTO exec_queue(task_id, kind, status) VALUES(?, 'exec', 'queued')",
              (tid,))
    c.commit()
    monkeypatch.setattr("test_agent.graph.builder.run_task", lambda _tid: None)
    real = queue.get_conn
    calls = {"n": 0}

    def flaky(path=None):
        calls["n"] += 1
        if calls["n"] == 2:                      # 第 2 次=收尾写入
            raise sqlite3.OperationalError("database is locked")
        return real(dbp)

    monkeypatch.setattr(queue, "get_conn", flaky)
    w = queue.QueueWorker()
    assert w._step() is True, "收尾失败不得把 worker 打崩"
    assert calls["n"] == 2
