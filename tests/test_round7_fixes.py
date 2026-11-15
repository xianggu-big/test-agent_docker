"""第七轮修复锁（2026-11-15 闲时找bug 第七轮，R24 clone/analyze 源头层 +
R23 前端守卫——后端部分）。

实锤六处（后端）：
1. clone 重跑 shutil.rmtree 无 onerror——Windows 下 git 只读 .git 对象让 git 任务
   重跑必炸 PermissionError（A4 允许重跑，宿主是 Windows）；
2. git clone 超时抛 TimeoutExpired 不走 codeload 回退（回退正是为"代理挂起"场景
   设计的）；
3. cli.py run：两阶段任务被拉去跑单阶段全图（跳过评审停等+队列纪律）；退出码
   恒 0（脚本无法判终态）；
4. analyze 主语言平局按插入序（=rglob 枚举序，重克隆可变）——重跑 primary 翻转，
   分级/镜像/报告不可比；
5. _validate_repo_url 放行带 userinfo 的 https URL——口令入库、git stderr 回显；
6. analyze/complexity 用绝对路径 parts 匹配 SKIP_DIRS——祖先目录名撞排除集
   （build/dist/venv/...）即全量误杀（db_probe.py:53 已修同型，两处未同步）；
   外加 SKIP_DIRS 与 SNAPSHOT_EXCLUDE_DIRS 两表漂移（.tox/target 等只挡容器快照
   不挡 LOC/热点/源码收集）。
"""
import os
import stat
from pathlib import Path

import pytest

from test_agent import config


# ---------- 1. 只读 .git 重跑 ----------

def test_force_rmtree_handles_readonly(tmp_path):
    from test_agent.graph.nodes import _force_rmtree
    target = tmp_path / "task_1"
    obj = target / ".git" / "objects" / "ab"
    obj.mkdir(parents=True)
    f = obj / "cdef"
    f.write_bytes(b"blob")
    os.chmod(f, stat.S_IREAD)                     # git loose object 形态（Windows 只读）
    try:
        _force_rmtree(target)                     # 旧 shutil.rmtree 在此 PermissionError
    finally:
        if f.exists():
            os.chmod(f, stat.S_IWRITE)
    assert not target.exists()


# ---------- 2. clone 超时走回退 ----------

def test_git_clone_timeout_returns_none_for_fallback(monkeypatch, tmp_path):
    """超时（挂起型代理）必须与 returncode!=0 同路返回 None → codeload 回退可用。"""
    from test_agent.graph import nodes
    import subprocess as _sp

    def boom(*a, **kw):
        raise _sp.TimeoutExpired(cmd="git", timeout=300)

    monkeypatch.setattr(nodes.subprocess, "run", boom)
    assert nodes._git_clone("https://github.com/o/r", tmp_path / "ws") is None


def test_git_clone_ok_returns_proc(monkeypatch, tmp_path):
    from test_agent.graph import nodes

    class _R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(nodes.subprocess, "run", lambda *a, **kw: _R())
    r = nodes._git_clone("https://github.com/o/r", tmp_path / "ws")
    assert r is not None and r.returncode == 0


# ---------- 3. cli run 两阶段拒绝 + 退出码 ----------

def test_cli_run_refuses_two_stage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,two_stage) VALUES(?,?,0.7,'ready_for_review',1,1)",
                    (pid, uid)).lastrowid
    c.commit()
    c.close()
    import cli
    called = []
    monkeypatch.setattr(cli, "run_task", lambda t: called.append(t))
    monkeypatch.setattr("sys.argv", ["cli", "run", str(tid)])
    rc = cli.main()
    assert rc == 1 and not called, "两阶段任务 cli run 必须拒绝（防绕过评审停等）"
    assert "两阶段" in capsys.readouterr().out


def test_cli_run_exit_code_reflects_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,two_stage) VALUES(?,?,0.7,'failed',1,0)",
                    (pid, uid)).lastrowid
    c.commit()
    c.close()
    import cli
    monkeypatch.setattr(cli, "run_task",
                        lambda t: {"verdict": "stop", "stop_reason": "疑似产品缺陷"})
    monkeypatch.setattr("sys.argv", ["cli", "run", str(tid)])
    rc = cli.main()
    assert rc == 2, "非 pass 终态必须非零退出码（脚本可判成败）"
    monkeypatch.setattr(cli, "run_task", lambda t: {"verdict": "pass"})
    rc = cli.main()
    assert rc == 0


# ---------- 4. 主语言平局确定性 ----------

def test_pick_primary_tie_deterministic():
    from test_agent.graph.nodes import _pick_primary
    assert _pick_primary({"python": 100, "javascript": 100}) == \
        _pick_primary({"javascript": 100, "python": 100}) == "javascript"
    assert _pick_primary({"python": 200, "javascript": 100}) == "python"
    assert _pick_primary({}) == "unknown"


# ---------- 5. userinfo URL 拒绝 ----------

def test_validate_repo_url_rejects_userinfo():
    from test_agent.graph.nodes import _validate_repo_url
    with pytest.raises(RuntimeError):
        _validate_repo_url("https://user:token@github.com/o/r")
    _validate_repo_url("https://github.com/o/r")          # 正常形态不误伤
    _validate_repo_url("git@github.com:o/r.git")


# ---------- 6. 相对路径 parts + 排除表对齐 ----------

def test_complexity_scans_under_excluded_named_ancestor(tmp_path):
    """仓库 checkout 到名为 venv/build 的祖先目录下不得全量误杀（db_probe 同型）。"""
    from test_agent.skills.complexity import complexity_hotspots
    ws = tmp_path / "venv" / "task_ws"
    ws.mkdir(parents=True)
    hot = ws / "hot.py"
    hot.write_text(
        "def f(a, b, c, d, e, g, h):\n"
        "    n = 0\n"
        "    if a:\n        n += 1\n    if b:\n        n += 2\n"
        "    if c:\n        n += 3\n    if d:\n        n += 4\n"
        "    if e:\n        n += 5\n    if g:\n        n += 6\n"
        "    if h:\n        n += 7\n    if n > 3:\n        n += 8\n"
        "    if n > 8:\n        n += 9\n    if n > 12:\n        n += 10\n"
        "    return n\n",   # 11 个分支 ≈ cc 12 = C 档（_HOT_RANKS 从 C 起）
        encoding="utf-8")
    hs = complexity_hotspots(ws)
    assert any(h["file"] == "hot.py" for h in hs), \
        "祖先目录名撞 SKIP_DIRS（绝对路径 parts 误杀）——热点应为空是 bug"


def test_skip_dirs_superset_of_snapshot_excludes():
    """SKIP_DIRS（本地快照/LOC/热点/源码收集共用）必须 ⊇ SNAPSHOT_EXCLUDE_DIRS——
    两表漂移会让 .tox/target 只挡容器快照、不挡 LOC 与热点扫描。"""
    missing = config.SNAPSHOT_EXCLUDE_DIRS - config.SKIP_DIRS
    assert not missing, f"SKIP_DIRS 缺：{sorted(missing)}"
