"""第六轮修复锁（2026-11-15 闲时找bug 第六轮，R20 图表层 + R21 消息/会话协议）。

实锤七处：
1. 覆盖率趋势图把 coverage=None 的轮画成 0%——假跳水（实际是"无数据"）；改 NaN 断点。
2. gen_charts 重跑不清理旧 PNG——本轮无 trace 时旧趋势图残留被 /charts 回读，新报告配旧图。
3. charts 生成失败（非 ok）零留痕静默 fail-open（对照 review_report 失败有留痕）。
4. /charts 单文件读失败（删除竞态/Windows 写锁）→ 端点 500 → 前端四图全灭。
5. /logs ORDER BY s.id DESC（会话块倒序）× 前端 slice(-40)（取数组尾）——两阶段重跑
   时日志面板显示的是上一轮旧会话尾部。
6. 会话收口家族：D1 代审 approve 路径 / 图异常路径（run_task/prep/exec）/ L4 全程
   ——agent_sessions 行永久悬开（ended_at IS NULL）。
7. record_spotcheck 推翻恢复：用例已被阶段一重跑删除时 UPDATE 命中 0 行仍虚报
   restored（前端 toast"已恢复"，实际无从恢复）。
"""
import json
import math
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent import config


# ---------- 1. coverage=None 断点 ----------

def test_coverage_series_none_round_is_nan_gap():
    from test_agent.skills.evaluate import _coverage_series
    xs, ys = _coverage_series([
        {"round": 1, "coverage": 0.5},
        {"round": 2, "coverage": None},
        {"round": 3, "coverage": 0.6}])
    assert xs == [1, 2, 3]
    assert ys[0] == 50 and math.isnan(ys[1]) and ys[2] == 60, \
        "None 覆盖率必须画断点（NaN），不是假 0%"


# ---------- 2. gen_charts 清旧图 ----------

def test_gen_charts_clears_stale_pngs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.skills.evaluate import gen_charts
    stale = tmp_path / "task_9_coverage_trend.png"
    stale.write_bytes(b"OLD")
    made = gen_charts(9, {}, {"matrix": []}, out_dir=tmp_path)
    assert not stale.exists(), "旧图必须清理——新报告不得配上一轮陈图"
    assert made == []


# ---------- 3. charts 失败留痕（结构锁） ----------

def test_charts_failure_leaves_trace():
    src = (Path(config.__file__).parent / "graph" / "nodes.py").read_text("utf-8")
    assert "[review·charts] 图表生成失败" in src, \
        "gen_charts 非 ok 分支必须留痕（对照 review_report 的 fail-open 留痕纪律）"


# ---------- 4. /charts 单文件容错 ----------

def test_charts_endpoint_skips_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                        "confirmed) VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid
        c.commit()
        c.close()
        charts = tmp_path / "reports" / "charts"
        charts.mkdir(parents=True)
        (charts / f"task_{tid}_good.png").write_bytes(b"PNG-good")
        (charts / f"task_{tid}_bad.png").write_bytes(b"PNG-bad")
        import pathlib
        orig = pathlib.Path.read_bytes

        def flaky(self):
            if "bad" in self.name:
                raise FileNotFoundError("删除竞态模拟")
            return orig(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", flaky)
        resp = client.get(f"/api/tasks/{tid}/charts", headers=h)
        assert resp.status_code == 200, "单文件读失败不得 500 全灭"
        names = [x["name"] for x in resp.json()]
        assert names == [f"task_{tid}_good.png"]


# ---------- 5. /logs 顺序 ----------

def test_logs_latest_session_messages_last(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                        "confirmed) VALUES(?,?,0.7,'failed',1)", (pid, uid)).lastrowid
        s1 = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
        c.execute("INSERT INTO agent_messages(session_id,seq,role,content) "
                  "VALUES(?,0,'assistant','[旧轮] 上一轮的日志')", (s1,))
        s2 = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
        c.execute("INSERT INTO agent_messages(session_id,seq,role,content) "
                  "VALUES(?,0,'assistant','[新轮] 本轮克隆开始')", (s2,))
        c.commit()
        c.close()
        logs = client.get(f"/api/tasks/{tid}/logs", headers=h).json()
        assert logs, "日志非空"
        assert "[新轮]" in logs[-1]["content"], \
            "数组尾部必须是最新会话消息（前端 slice(-40) 取尾部=当前轮日志）"


# ---------- 6. 会话收口 ----------

class _RaisingGraph:
    def invoke(self, init, cfg):
        raise RuntimeError("节点异常（模拟）")


class _FakeRunner:
    def cleanup(self, name):
        pass


def test_run_task_exception_closes_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed) VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    c.commit()
    from test_agent.graph import builder
    monkeypatch.setattr(builder, "build_graph", lambda deps: _RaisingGraph())
    monkeypatch.setattr(builder, "make_deps",
                        lambda: types.SimpleNamespace(conn=db.get_conn(),
                                                      runner=_FakeRunner()))
    with pytest.raises(RuntimeError):
        builder.run_task(tid)
    row = c.execute(
        "SELECT COUNT(*) AS n FROM agent_sessions WHERE task_id=? AND ended_at IS NULL",
        (tid,)).fetchone()["n"]
    assert row == 0, "图异常上抛前必须收口 session（ended_at）"


def test_d1_approve_closes_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    uid = c.execute("INSERT INTO users(username,password_hash,role) "
                    "VALUES('u','x','developer')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,ai_reviewer,two_stage) VALUES(?,?,0.7,'queued',0,'auto',1)",
                    (pid, uid)).lastrowid
    c.commit()
    from test_agent.graph import builder
    # 零 Docker 纪律（2026-11-20）：run_task_auto :624 make_deps 构造 SandboxRunner
    # 会探活 daemon——Docker 未启动时此测试环境性失败，patch 掉
    monkeypatch.setattr(builder, "make_deps",
                        lambda: types.SimpleNamespace(conn=db.get_conn(),
                                                      runner=_FakeRunner()))
    # reviewer_llm/run_d1_review 是 run_task_auto 内的局部 import——补丁源模块
    monkeypatch.setattr("test_agent.skills.ai_reviewer.reviewer_llm", lambda: object())
    monkeypatch.setattr("test_agent.skills.ai_reviewer.run_d1_review",
                        lambda llm, conn, t, req, repo: {"decision": "approve", "reason": "ok"})
    monkeypatch.setattr(builder, "run_task_prep", lambda t: {})
    # A1 放行门契约：循环返回 outcome dict（None 会判 fail_closed 提前 return）
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "converged", "reason": ""})
    builder.run_task_auto(tid)
    row = c.execute(
        "SELECT COUNT(*) AS n FROM agent_sessions WHERE task_id=? AND ended_at IS NULL",
        (tid,)).fetchone()["n"]
    assert row == 0, "D1 approve 路径也必须收口 session（旧实现只在拒绝/失败路径关）"


def test_l4_closes_session_source_lock():
    src = (Path(config.__file__).parent / "graph" / "l4.py").read_text("utf-8")
    assert "finally:" in src and "UPDATE agent_sessions SET ended_at" in src, \
        "L4 会话必须 try/finally 收口（成功/诚实跳过/异常三路都不悬开）"


# ---------- 7. spotcheck 恢复如实 ----------

def test_spotcheck_override_missing_case_no_fake_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.store import db
    from test_agent.skills import ai_reviewer as ar
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,?)",
                       (pid, uid)).lastrowid
    sid = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash) "
        "VALUES(?, 'pytest', 1, 'test_gen_r1.py', 'x', 'h')", (tid,)).lastrowid
    cid = conn.execute("INSERT INTO test_cases(script_id,source,name,method,content,enabled) "
                       "VALUES(?, 'ai', 'test_x', '等价类', 'x', 0)", (sid,)).lastrowid
    did = conn.execute(
        "INSERT INTO review_decisions(task_id,reviewer_role,decision,target,reasoning,"
        "evidence,round) VALUES(?, 'ai_proxy', 'reject', 'test_x', 'r', 'e', 1)",
        (tid,)).lastrowid
    conn.commit()
    # 阶段一重跑把用例整删（delete-first 家族）——决策行悬挂
    conn.execute("DELETE FROM test_cases WHERE id=?", (cid,))
    conn.commit()
    out = ar.record_spotcheck(conn, did, "override", "用例其实没问题", uid)
    assert out["restored"] is None, "用例已不存在——不得虚报'已恢复'"
