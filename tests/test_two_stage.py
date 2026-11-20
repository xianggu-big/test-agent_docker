"""两阶段人机协作（docs/两阶段人机协作流程设计-20261031.md）：
状态机迁移（CHECK 重建）/ 两图守卫 / feedback 预算 / 布考场重建文件 / 顺延收集闸。
铁律：pytest 不触碰真实库——本文件全部走 tmp 库（monkeypatch config.DB_PATH）。"""
import sqlite3
from pathlib import Path

import pytest

from test_agent import config
from test_agent.graph import builder
from test_agent.skills.gen_tests import (collect_gate_files, rebuild_gen_files_from_db)
from test_agent.store import db


# ---------- 老库形态（df8d573 时代的 test_tasks DDL：CHECK 无 ready_for_review） ----------
OLD_TASKS_DDL = """CREATE TABLE IF NOT EXISTS test_tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id       INTEGER NOT NULL REFERENCES projects(id),
  user_id          INTEGER NOT NULL REFERENCES users(id),
  requirement_text TEXT,
  coverage_target  REAL,
  status           TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','cloning','analyzing','provisioning',
                                     'generating','running','reporting',
                                     'done','failed','cancelled')),
  confirmed        INTEGER NOT NULL DEFAULT 0,
  auto_prepare     INTEGER NOT NULL DEFAULT 0,
  confirmed_by     INTEGER REFERENCES users(id),
  confirmed_at     TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);"""


def _seed_user_project(conn) -> int:
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    conn.commit()
    return uid


def test_old_db_rebuild_preserves_data_and_accepts_new_status(tmp_path, monkeypatch):
    """CHECK 枚举无法 ALTER：老库 init_db 触发 12 步法重建表——数据零丢失 + 新状态可写。"""
    p = tmp_path / "old.db"
    conn = db.get_conn(p)
    # 最小老形态库：users/projects 先建（老 DDL 的 FK 指向它们），test_tasks 用旧 CHECK
    conn.executescript(
        "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, password_hash TEXT);"
        "CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,"
        " repo_url TEXT, name TEXT);" + OLD_TASKS_DDL)
    uid = _seed_user_project(conn)
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed) "
        "VALUES (?, ?, '老任务需求', 1)", (1, uid)).lastrowid
    conn.commit()
    assert db._tasks_status_check_outdated(conn)
    conn.close()

    assert db.init_db(p) == 37
    conn = db.get_conn(p)
    row = conn.execute("SELECT * FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert row["requirement_text"] == "老任务需求" and row["confirmed"] == 1
    assert row["two_stage"] == 0 and row["ai_reviewer"] == "off"          # 补列默认值
    # 新 CHECK 生效：ready_for_review 可写
    conn.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    conn.commit()
    assert conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()["status"] \
        == "ready_for_review"
    # 自增序列保留：新插入不撞 id
    tid2 = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id) VALUES (?, ?)", (1, uid)).lastrowid
    assert tid2 > tid
    # FK 仍生效（重建后其他表 REFERENCES 指回新表）
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (9999, 'test_agent')")
    conn.close()


def test_fresh_db_accepts_ready_for_review(tmp_path):
    p = tmp_path / "t.db"
    db.init_db(p)
    conn = db.get_conn(p)
    uid = _seed_user_project(conn)
    conn.execute("INSERT INTO test_tasks(project_id, user_id, status) VALUES (?, ?, 'ready_for_review')",
                 (1, uid))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM test_tasks").fetchone()["n"] == 1
    conn.close()


def test_prep_graph_compiles(tmp_path):
    """两图结构在 LangGraph 层可编译（边/条件路由写错在 compile 期暴露）。"""
    conn = db.get_conn(tmp_path / "g.db")
    deps = builder.GraphDeps(conn=conn, runner=object(), registry=object(), skills={})
    prep = builder.build_prep_graph(deps)
    exec_g = builder.build_exec_graph(deps)
    prep_nodes = set(prep.get_graph().nodes) - {"__end__", "__start__"}
    exec_nodes = set(exec_g.get_graph().nodes) - {"__end__", "__start__"}
    assert prep_nodes == {"clone", "analyze", "plan", "generate", "static_draft"}
    assert exec_nodes == {"provision", "generate", "run", "evaluate", "report", "review"}
    conn.close()


def test_prep_gen_route_retry_once():
    """出题 0 条重试一次（单阶段 repair_gen 的两阶段对等物，E2E task_264 实锤后补）：
    0 条且未到轮次上限→重试；仍 0 到上限→停等（评审页对话框是自愈通道）；有产出→直接过。"""
    from test_agent import config as cfg
    route = builder.prep_gen_route
    assert route(0, 1, cfg.PREP_GEN_MAX_ROUNDS) == "generate"    # 首轮 0 条 → 重试
    assert route(0, cfg.PREP_GEN_MAX_ROUNDS, cfg.PREP_GEN_MAX_ROUNDS) == "__end__"  # 重试仍 0 → 停
    assert route(5, 1, cfg.PREP_GEN_MAX_ROUNDS) == "__end__"     # 有产出 → 不重试


@pytest.fixture()
def task_env(tmp_path, monkeypatch):
    """tmp 库 + tmp DATA_DIR + 一个 queued 任务；返回 (conn, tid, ws路径)。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    conn = db.get_conn()
    uid = _seed_user_project(conn)
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed, two_stage) "
        "VALUES (?, ?, '需求', 1, 1)", (1, uid)).lastrowid
    conn.commit()
    yield conn, tid, tmp_path / "workspaces" / f"task_{tid}"
    conn.close()


def test_run_task_exec_guards(task_env):
    conn, tid, ws = task_env
    # 状态不是 ready_for_review：拒（放行锁语义）。2026-11-15 状态机四断点后 queued
    # 也是合法放行态（api_run 放行即置 queued，锁同步放行）——非法态用 done 验证；
    # queued 被锁接受的行为锁在 tests/test_state_machine_fixes.py::test_exec_lock_accepts_queued
    conn.execute("UPDATE test_tasks SET status='done' WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="待审查"):
        builder.run_task_exec(tid)
    # two_stage=0：拒（单阶段任务走原下发）
    conn.execute("UPDATE test_tasks SET status='ready_for_review', two_stage=0 WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="两阶段"):
        builder.run_task_exec(tid)
    # 未确认：fail-closed
    conn.execute("UPDATE test_tasks SET two_stage=1, confirmed=0 WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="fail-closed"):
        builder.run_task_exec(tid)


def test_run_task_exec_requires_workspace(task_env):
    conn, tid, ws = task_env
    conn.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="工作区不存在"):
        builder.run_task_exec(tid)
    assert conn.execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()["status"] \
        == "failed"      # 如实落库，不做伪装


def test_run_task_prep_unconfirmed(task_env):
    conn, tid, ws = task_env
    conn.execute("UPDATE test_tasks SET confirmed=0 WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="fail-closed"):
        builder.run_task_prep(tid)


# ---------- feedback 预算守卫（两阶段③） ----------

def test_feedback_round_guards(task_env, monkeypatch):
    conn, tid, ws = task_env
    from test_agent.graph.builder import run_feedback_round
    with pytest.raises(ValueError):                       # 空反馈
        run_feedback_round(tid, "  ")
    with pytest.raises(RuntimeError, match="待审查"):      # 状态门
        run_feedback_round(tid, "补一下边界用例")
    conn.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(RuntimeError, match="工作区"):      # 阶段一未完成
        run_feedback_round(tid, "补一下")
    ws.mkdir(parents=True)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        run_feedback_round(tid, "补一下")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    for i in range(config.FEEDBACK_MAX_ROUNDS):           # 预算封顶：第 6 轮拒
        (ws / f"test_gen_fb{i + 1}.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="预算"):
        run_feedback_round(tid, "补一下")


# ---------- 布考场：按审查结论重写生成文件 + 顺延收集闸 ----------

def _seed_gen_script(conn, tid, filename, cases):
    """cases: [(name, enabled)] → 返回 script_id；content 含 import + 两个测试函数。"""
    body = "import math\n\n\n" + "\n\n\n".join(
        f"def {name}():\n    assert 1\n" for name, _ in cases)
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, filename, content, content_hash)
           VALUES (?, 'pytest', 1, ?, ?, 'h')""", (tid, filename, body))
    for name, enabled in cases:
        conn.execute(
            "INSERT INTO test_cases(script_id, source, name, enabled, content) VALUES (?, 'ai', ?, ?, ?)",
            (cur.lastrowid, name, enabled, f"def {name}():\n    assert 1\n"))
    conn.commit()
    return cur.lastrowid


def test_rebuild_gen_files_from_db(task_env):
    conn, tid, ws = task_env
    ws.mkdir(parents=True)
    _seed_gen_script(conn, tid, "test_gen_r1.py",
                     [("test_keep", 1), ("test_drop", 0)])
    assert rebuild_gen_files_from_db(conn, tid, ws) == 1
    text = (ws / "test_gen_r1.py").read_text(encoding="utf-8")
    assert "def test_keep" in text
    assert "def test_drop" not in text           # 已停用用例摘除
    assert "import math" in text                 # imports 保留（丢了整文件废）
    # 勾回来 → 重建恢复
    conn.execute("UPDATE test_cases SET enabled=1 WHERE name='test_drop'")
    conn.commit()
    rebuild_gen_files_from_db(conn, tid, ws)
    assert "def test_drop" in (ws / "test_gen_r1.py").read_text(encoding="utf-8")


def test_collect_gate_files_drops_uncollectable(task_env):
    conn, tid, ws = task_env
    ws.mkdir(parents=True)
    (ws / "test_gen_r1.py").write_text("def test_ok():\n    assert 1\n", encoding="utf-8")
    (ws / "test_gen_fb1.py").write_text("import 不存在模块\n", encoding="utf-8")
    sid = _seed_gen_script(conn, tid, "test_gen_fb1.py", [("test_bad", 1)])

    class FakeRunner:
        def exec_in(self, container, cmd, timeout_s=0):
            fname = cmd[-1]
            ok = fname != "test_gen_fb1.py"

            class R:
                exit_code = 0 if ok else 1
                stdout, stderr = "", "ModuleNotFoundError"
            return R()

    from test_agent.skills.gen_tests import collect_gate_files as _cgf
    dropped = _cgf(FakeRunner(), object(), conn, tid, ws)
    assert dropped == ["test_gen_fb1.py"]
    assert not (ws / "test_gen_fb1.py").exists()
    assert (ws / "test_gen_r1.py").exists()
    assert conn.execute("SELECT COUNT(*) AS n FROM test_cases WHERE script_id=?",
                        (sid,)).fetchone()["n"] == 0          # 废案删用例行
    assert conn.execute("SELECT COUNT(*) AS n FROM test_scripts WHERE id=?",
                        (sid,)).fetchone()["n"] == 0          # 废案删脚本行


def test_toggle_ai_case_allowed_only_at_ready_for_review(task_env):
    """评审页"取消用例"：AI 用例启停仅待审查态开放（执行链路不可变），布考场重建生效。"""
    conn, tid, ws = task_env
    ws.mkdir(parents=True)
    sid = _seed_gen_script(conn, tid, "test_gen_r1.py", [("test_a", 1)])
    case_id = conn.execute("SELECT id FROM test_cases WHERE script_id=?", (sid,)).fetchone()["id"]
    from test_agent.api import server
    import fastapi
    # queued 态：AI 用例不可启停（409）
    conn.execute("UPDATE test_tasks SET status='queued' WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(fastapi.HTTPException) as ei:
        server.api_toggle_case(case_id, None)
    assert ei.value.status_code == 409
    # 待审查态：可停用（布考场重建后文件摘除）
    conn.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    conn.commit()
    resp = server.api_toggle_case(case_id, None)
    assert resp["enabled"] == 0
    assert "def test_a" not in (ws / "test_gen_r1.py").read_text(encoding="utf-8")


def test_toggle_ai_case_without_workspace_defers_file(task_env):
    """工作区缺失（阶段一未完成/已清理）：评审结论落库即生效，文件重建顺延布考场——不炸 500
    （Playwright 验收实锤：seed 任务无工作区时 toggle 曾 FileNotFoundError）。库是唯一真源。"""
    conn, tid, ws = task_env
    assert not ws.exists()
    sid = _seed_gen_script(conn, tid, "test_gen_r1.py", [("test_a", 1)])
    case_id = conn.execute("SELECT id FROM test_cases WHERE script_id=?", (sid,)).fetchone()["id"]
    conn.execute("UPDATE test_tasks SET status='ready_for_review' WHERE id=?", (tid,))
    conn.commit()
    from test_agent.api import server
    resp = server.api_toggle_case(case_id, None)
    assert resp["enabled"] == 0 and resp.get("file_deferred") is True
    assert conn.execute("SELECT enabled FROM test_cases WHERE id=?", (case_id,)).fetchone()["enabled"] == 0


def test_feedback_summary_carries_case_details(tmp_path, monkeypatch):
    """增补结果下拉的数据源契约（晚间用户实测：前端「本轮新增」面板消费 b.cases，
    但 gen_tests summary 曾无此字段=面板永远空）。fake-LLM 走通 generate_tests，
    断言 summary.cases 每条含 name/method/expected/input/expected_basis。"""
    import json as _json
    from types import SimpleNamespace
    from test_agent.skills import gen_tests as gt

    class FakeLLM:
        def invoke(self, messages):
            return SimpleNamespace(content=_json.dumps({"imports": "from calc import add",
                "cases": [{"name": "test_add_补边界", "method": "边界值分析",
                           "input": "add(0,1)", "expected": "返回 1",
                           "expected_basis": "用户反馈",
                           "code": "def test_add_补边界():\n    assert add(0, 1) == 1"}]},
                ensure_ascii=False), usage_metadata=None)
    class RobustFake:                     # B5 退避包装形态：.invoke 可调
        def __init__(self, inner): self.inner = inner
        def invoke(self, messages): return self.inner.invoke(messages)
    monkeypatch.setattr(gt, "make_chat", lambda **kw: RobustFake(FakeLLM()))

    class FakeRunner:                       # client 抛错 → collect 闸顺延（阶段一出题形态）
        @property
        def client(self):
            raise RuntimeError("no docker in test")

    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name, primary_language) VALUES (1, 'r', 'n', 'python')")
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id, confirmed) VALUES (1, ?, 1)", (uid,)).lastrowid
    conn.commit()
    out = _json.loads(gt.generate_tests("k", {"workspace": str(ws), "requirement": "需求",
                                              "module_hint": "python", "container_name": "none",
                                              "round_no": 1, "feedback": "补一条边界",
                                              "fb_no": 1, "version": 101}, tid, FakeRunner()))
    assert out["generated"] == 1 and len(out["cases"]) == 1
    case = out["cases"][0]
    assert case["name"].startswith("test_") and case["method"] == "边界值分析"
    assert case["expected"] == "返回 1" and case["expected_basis"] == "用户反馈" and "input" in case
    conn.close()


def test_startup_sweeps_orphan_active_tasks(tmp_path, monkeypatch):
    """启动清扫（348 变砖实锤）：重启后 ACTIVE 态任务自动 failed+重跑指引；ready_for_review 不动。"""
    from test_agent.api import server
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    for i, st in enumerate(("cloning", "running", "ready_for_review", "done")):
        conn.execute("INSERT INTO test_tasks(project_id, user_id, status) VALUES (1, ?, ?)", (uid, st))
    conn.commit()
    server._sweep_orphan_active_tasks()
    states = {r["status"] for r in conn.execute("SELECT status FROM test_tasks").fetchall()}
    assert "cloning" not in states and "running" not in states          # ACTIVE 已清扫
    assert "ready_for_review" in states and "done" in states            # 停等/终态不动
    assert conn.execute("SELECT COUNT(*) AS n FROM test_tasks WHERE status='failed'").fetchone()["n"] == 2
    conn.close()
