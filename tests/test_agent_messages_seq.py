# -*- coding: utf-8 -*-
"""B2（2026-11-27 总包）：agent_messages.seq 竞态/重用红测试。

旧口径 `seq = COUNT(*) WHERE session_id=?` 两类坏法：
①并发写同一会话：两连接同时 COUNT 到同一 n → UNIQUE(session_id,seq) 撞
  IntegrityError（真崩溃）；
②删除过消息后 COUNT 偏小 → 新 seq 重用已被占用的序号（同撞 UNIQUE）。
修法：INSERT 内联 `(SELECT COALESCE(MAX(seq),-1)+1 ...)` 子查询——同一条语句内
取号，SQLite 单写锁下原子。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _env(tmp_path):
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "t.db"
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid
    c.execute("INSERT INTO agent_sessions(task_id, started_at) VALUES(?, datetime('now'))",
              (tid,))
    sid = c.execute("SELECT id FROM agent_sessions WHERE task_id=?", (tid,)).fetchone()[0]
    c.commit()
    return c, sid


def test_append_survives_deleted_middle_row(tmp_path):
    """删除中部消息后 COUNT(*) 偏小 → 旧口径 seq 撞 UNIQUE；新口径 MAX+1 绕开。"""
    from test_agent.store.db import append_agent_message
    c, sid = _env(tmp_path)
    for i in range(3):
        append_agent_message(c, sid, "assistant", f"m{i}")
    c.execute("DELETE FROM agent_messages WHERE session_id=? AND seq=1", (sid,))
    c.commit()
    append_agent_message(c, sid, "user", "after-delete")       # 旧口径此处 IntegrityError
    seqs = [r[0] for r in c.execute(
        "SELECT seq FROM agent_messages WHERE session_id=? ORDER BY seq", (sid,)).fetchall()]
    assert seqs == [0, 2, 3]


def test_append_unique_under_interleaved_writers(tmp_path):
    """两连接交错写同一会话（竞态形态）：所有 seq 唯一且连续。"""
    from test_agent.store.db import append_agent_message
    c1, sid = _env(tmp_path)
    import sqlite3
    dbp = c1.execute("PRAGMA database_list").fetchall()[0][2]
    c2 = sqlite3.connect(dbp, timeout=10)
    for i in range(5):
        append_agent_message(c1, sid, "assistant", f"a{i}")
        append_agent_message(c2, sid, "user", f"b{i}")
    rows = c1.execute("SELECT seq FROM agent_messages WHERE session_id=? ORDER BY seq",
                      (sid,)).fetchall()
    seqs = [r[0] for r in rows]
    assert len(seqs) == 10
    assert seqs == list(range(10))
