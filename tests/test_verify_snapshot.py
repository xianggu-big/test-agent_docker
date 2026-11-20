"""0-A 验证资产物化快照测试（2026-11-20 设计稿 docs/设计方案-AgentLoop文件级实施设计-20261120.md 包 0）。

G9 实锤的口径缺口：布考场从 DB 重写用例文件存在 repr 风格差（语义零改但字节不同），
整体指纹只能"发现被改"，不能定位文件、更不能恢复原样。0-A 三件：
- 物化快照：create_verify_task 时把冻结用例集逐文件字节复制到
  data/verify_freeze/task_{vid}/ + manifest.json（逐文件 sha256/行数/AST 用例名单/framework）；
- 布考场消费：verify_mode=1 优先从快照逐字节重写（restore_from_snapshot），
  快照缺失诚实回退 DB 重写+留痕（snapshot_missing）；
- 分文件对账（reconcile_snapshot）：终态 workspace vs manifest 逐文件 sha256——
  漂移定位到文件；style_drift=AST 语义等价（ast.dump 相同，G9 repr 形态豁免）/
  substantive_drift=实质改动（报文件名+行差）。
铁律：tmp 库、零真实图执行、零 Docker。restore 是治理动作（人工触发），不自动改库。
"""
import json

import pytest

from test_agent import config
from test_agent.skills.verify_fix import (create_verify_task, materialize_verify_snapshot,
                                          reconcile_snapshot, restore_from_snapshot,
                                          snapshot_dir_for)

PY_CONTENT = ("import calc\n"
              "\n"
              "\n"
              "def test_a():\n"
              "    assert calc.f(1) == 1\n"
              "\n"
              "\n"
              "def test_b():\n"
              "    assert calc.g(2) == 2\n")


def _make_task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                       "VALUES('u','x','developer')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "two_stage,verify_mode,requirement_text) VALUES(?,?,0.7,'ready_for_review',1,1,1,"
        "'[回归验证 of task 999] 原需求')", (pid, uid)).lastrowid
    conn.commit()
    return conn, tid, pid, uid


def _add_script(conn, tid, filename, content, framework="pytest"):
    cur = conn.execute(
        "INSERT INTO test_scripts(task_id, module_id, framework, version, filename,"
        " content, content_hash) VALUES (?,NULL,?,1,?,?,?)",
        (tid, framework, filename, content, "h"))
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content,"
                 " enabled, priority) VALUES (?,'ai','test_a','等价类','assert True',1,'P1')",
                 (cur.lastrowid,))
    conn.commit()
    return cur.lastrowid


def test_snapshot_materialized_on_create(tmp_path, monkeypatch):
    """创建验证任务即物化：快照目录 + manifest 逐文件 sha256 可复算 + 字节=DB content。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    d = snapshot_dir_for(vid)
    mf = d / "manifest.json"
    assert mf.exists(), "创建验证任务后冻结快照 manifest 必须存在"
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    assert manifest["verify_task_id"] == vid
    assert len(manifest["files"]) == 1
    entry = manifest["files"][0]
    assert entry["filename"] == "test_gen_r1.py"
    assert entry["framework"] == "pytest"
    assert entry["cases"] == ["test_a", "test_b"]
    import hashlib
    raw = (d / "test_gen_r1.py").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    assert raw.decode("utf-8") == PY_CONTENT  # 逐字节=DB 原文


def test_snapshot_idempotent(tmp_path, monkeypatch):
    """materialize 幂等：manifest 已存在时不重写（字节不变）。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    d = snapshot_dir_for(vid)
    before = (d / "manifest.json").read_bytes()
    r = materialize_verify_snapshot(conn, vid)
    assert r["ok"] is True
    assert (d / "manifest.json").read_bytes() == before


def test_restore_bytes_identical(tmp_path, monkeypatch):
    """恢复：工作区漂移文件从快照逐字节还原；mode=snapshot。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    (ws / "test_gen_r1.py").write_text("被改坏的占位\n", encoding="utf-8")
    r = restore_from_snapshot(conn, vid, ws)
    assert r["mode"] == "snapshot" and r["n_files"] == 1
    assert (ws / "test_gen_r1.py").read_bytes() == PY_CONTENT.encode("utf-8")


def test_restore_missing_snapshot_falls_back(tmp_path, monkeypatch):
    """快照缺失：诚实回退 DB 重写模式（mode=db_fallback），不伪装成功。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    import shutil
    shutil.rmtree(snapshot_dir_for(vid))   # 模拟老任务/物化失败
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    r = restore_from_snapshot(conn, vid, ws)
    assert r["mode"] == "db_fallback" and r["n_files"] == 0


def test_reconcile_match(tmp_path, monkeypatch):
    """终态对账：工作区与快照逐字节一致 → match、零漂移。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    (ws / "test_gen_r1.py").write_bytes(PY_CONTENT.encode("utf-8"))
    rec = reconcile_snapshot(conn, vid, ws)
    assert rec["status"] == "match" and rec["drift"] == []


def test_reconcile_substantive_drift(tmp_path, monkeypatch):
    """实质改动（AST 语义变了）：定位到文件 + 行差；不算风格差。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    drifted = PY_CONTENT.replace("assert calc.f(1) == 1", "assert calc.f(1) == 2")
    (ws / "test_gen_r1.py").write_text(drifted, encoding="utf-8")
    rec = reconcile_snapshot(conn, vid, ws)
    assert rec["status"] == "substantive_drift"
    assert rec["drift"][0]["file"] == "test_gen_r1.py"
    assert rec["drift"][0]["kind"] == "substantive_drift"


def test_reconcile_style_drift_whitelist(tmp_path, monkeypatch):
    """G9 形态豁免：repr/格式差但 AST 语义等价（ast.dump 相同）→ style_drift。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    # 行尾补语义不变注释：AST 完全等价（ast.dump 相同）、仅源码文本差（G9 repr 形态）
    styled = PY_CONTENT.replace("assert calc.f(1) == 1",
                                'assert calc.f(1) == 1  # 语义不变注释')
    (ws / "test_gen_r1.py").write_text(styled, encoding="utf-8")
    rec = reconcile_snapshot(conn, vid, ws)
    assert rec["status"] == "style_drift", f"AST 等价应豁免：{rec['drift']}"
    assert rec["drift"][0]["kind"] == "style_drift"


def test_reconcile_missing_snapshot_status(tmp_path, monkeypatch):
    """快照缺失：missing_snapshot（老验证任务无快照=如实上报，不硬猜）。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    import shutil
    shutil.rmtree(snapshot_dir_for(vid))
    rec = reconcile_snapshot(conn, vid, tmp_path / "workspaces" / f"task_{vid}")
    assert rec["status"] == "missing_snapshot"


def test_reconcile_unexpected_new_file(tmp_path, monkeypatch):
    """冻结下工作区冒出 manifest 外的新 test_gen 文件 = 异常如实上报。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    (ws / "test_gen_r1.py").write_bytes(PY_CONTENT.encode("utf-8"))
    (ws / "test_gen_new.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    rec = reconcile_snapshot(conn, vid, ws)
    kinds = {d["kind"] for d in rec["drift"]}
    assert "unexpected_new_file" in kinds
    assert rec["status"] == "substantive_drift"


def test_style_drift_syntax_error_is_substantive(tmp_path, monkeypatch):
    """漂移文件语法都炸了：无法证明语义等价 → substantive（保守方向，宁严勿宽）。"""
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch)
    _add_script(conn, tid, "test_gen_r1.py", PY_CONTENT)
    vid = create_verify_task(conn, tid)
    ws = tmp_path / "workspaces" / f"task_{vid}"
    ws.mkdir(parents=True)
    (ws / "test_gen_r1.py").write_text("def test_a(:\n  broken\n" * 5, encoding="utf-8")
    rec = reconcile_snapshot(conn, vid, ws)
    assert rec["status"] == "substantive_drift"
