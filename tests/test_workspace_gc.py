# -*- coding: utf-8 -*-
"""B7（2026-11-27 总包）：工作区自动回收单测（两例）。

①终态>14天+有冻结快照 → 删；②缺任一条件（非终态/太新/无快照）→ 保留。
旗标关=零行为（maybe_recycle_on_startup 返回 None）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _make_env(tmp_path):
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "test_agent.db"     # 与生产布局一致（workspace_gc 按此取只读连接）
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,created_at) VALUES(?,?,0.7,'failed',1,datetime('now','"
                    "-30 days'))", (pid, uid)).lastrowid
    c.commit()
    ws = tmp_path / "workspaces" / f"task_{tid}"
    ws.mkdir(parents=True)
    (ws / "main.py").write_text("x = 1", encoding="utf-8")
    import os
    old = time.time() - 20 * 24 * 3600
    os.utime(ws, (old, old))                      # 目录 mtime 拨回 20 天前
    return config, tmp_path, tid, c


def test_stale_terminal_with_snapshot_removed(tmp_path):
    config, tmp_path, tid, c = _make_env(tmp_path)
    from test_agent.skills.workspace_gc import recycle_stale_workspaces
    snap = tmp_path / "verify_freeze" / f"task_{tid}"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{}", encoding="utf-8")
    r = recycle_stale_workspaces(data_dir=tmp_path)
    assert tid in r["removed"]
    assert not (tmp_path / "workspaces" / f"task_{tid}").exists()


def test_without_snapshot_or_recent_or_running_kept(tmp_path):
    config, tmp_path, tid, c = _make_env(tmp_path)
    from test_agent.skills.workspace_gc import recycle_stale_workspaces
    # ①终态但无快照 → 保留（无退路不删）
    r1 = recycle_stale_workspaces(data_dir=tmp_path)
    assert tid not in r1["removed"]
    assert r1["skipped"]["no_snapshot"] >= 1
    # ②有快照但目录是新的（mtime=现在） → 保留
    snap = tmp_path / "verify_freeze" / f"task_{tid}"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{}", encoding="utf-8")
    import os
    os.utime(tmp_path / "workspaces" / f"task_{tid}", None)   # mtime=现在
    r2 = recycle_stale_workspaces(data_dir=tmp_path)
    assert tid not in r2["removed"]
    assert r2["skipped"]["recent"] >= 1
    # ③非终态任务 → 保留
    c.execute("UPDATE test_tasks SET status='running' WHERE id=?", (tid,))
    c.commit()
    old = time.time() - 20 * 24 * 3600
    os.utime(tmp_path / "workspaces" / f"task_{tid}", (old, old))
    r3 = recycle_stale_workspaces(data_dir=tmp_path)
    assert tid not in r3["removed"]
    assert r3["skipped"]["non_terminal"] >= 1


def test_flag_off_is_noop(tmp_path):
    from test_agent.skills.workspace_gc import maybe_recycle_on_startup
    assert maybe_recycle_on_startup() is None      # 默认关=零行为
