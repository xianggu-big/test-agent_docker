"""A2 验证任务冻结测试（2026-11-20 设计方案 docs/设计方案-文件级修复与验收设计-20261120.md 包 A）。

复现 Qoder 体检论断 2：create_verify_task 复制原用例后 two_stage=1 走普通执行图，
exec 图 evaluate 非 pass/stop → generate（repair_gen）、review repair_gen → generate
无 verify 分流——验证任务第二轮调用生成器（delete-first 删原用例重生成），破坏
"用相同考卷比较修复前后"的前提。

修复契约（回应 Qoder 残片边界 1：保护所有用例写入口，不能只禁止生成节点）：
- test_tasks.verify_mode 列贯穿 任务行 → TaskState → 执行图路由；
- 图级拦截：evaluate 非 pass/stop → report（不补题不重跑）、review repair_gen → END；
- 写入口清单全部不可达（generate 主生成/fbN 增补/剪除/重写/delete-first）；
- 用例集指纹（sha1）执行前后留痕 agent_messages = 审计证据（不代替写入拦截）。
铁律：tmp 库、零真实图执行、零 Docker。"""
import types

import pytest

from test_agent import config

VERIFY_PREFIX = "[回归验证 of task 999]"


def _make_task(tmp_path, monkeypatch, *, status="ready_for_review", verify_mode=1,
               requirement=None):
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
        "two_stage,verify_mode,requirement_text) VALUES(?,?,0.7,?,1,1,?,?)",
        (pid, uid, status, verify_mode,
         requirement if requirement is not None else VERIFY_PREFIX + " 原需求")).lastrowid
    conn.commit()
    return conn, tid, pid, uid


# ---------- 迁移：列存在 + 历史前缀任务回填 ----------

def test_migration_adds_verify_mode_column(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    conn = db.get_conn()
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(test_tasks)")}
    assert "verify_mode" in cols, "迁移必须补 verify_mode 列"
    assert cols["verify_mode"]["dflt_value"] in ("0", 0), "默认 0（非验证任务行为不变）"


def test_migration_backfills_historical_prefix_tasks(tmp_path, monkeypatch):
    """历史验证任务（verify_mode=0 但 requirement 带软链前缀）迁移时回填 1。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()   # 先建库（列已在）
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                       "VALUES('u','x','developer')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    conn.execute("INSERT INTO test_tasks(project_id,user_id,status,confirmed,verify_mode,"
                 "requirement_text) VALUES(?,?,'ready_for_review',1,0,?)",
                 (pid, uid, VERIFY_PREFIX + " 历史"))
    conn.commit()
    db.init_db()   # 触发 _migrate 回填
    row = conn.execute("SELECT verify_mode FROM test_tasks WHERE requirement_text LIKE ?",
                       (VERIFY_PREFIX + "%",)).fetchone()
    assert row["verify_mode"] == 1, "历史前缀任务必须回填 verify_mode=1"
    normal = conn.execute("SELECT COUNT(*) AS n FROM test_tasks WHERE verify_mode=1 "
                          "AND requirement_text NOT LIKE '[回归验证 of task %'").fetchone()["n"]
    assert normal == 0, "普通任务不得被误回填"


# ---------- create_verify_task 打标 ----------

def test_create_verify_task_sets_verify_mode(tmp_path, monkeypatch):
    conn, tid, pid, uid = _make_task(tmp_path, monkeypatch, status="done", verify_mode=0,
                                     requirement="普通需求")
    # 原任务放一条用例（复制源）
    sid = conn.execute("INSERT INTO test_scripts(task_id, framework, filename, content, content_hash)"
                       " VALUES(?, 'pytest', 't.py', 'def t(): pass', 'h0')", (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id, source, name, content, enabled) "
                 "VALUES(?, 'ai', 'c1', 'assert 1', 1)", (sid,))
    conn.commit()
    from test_agent.skills.verify_fix import create_verify_task
    vid = create_verify_task(conn, tid)
    row = conn.execute("SELECT verify_mode, two_stage, status FROM test_tasks WHERE id=?",
                       (vid,)).fetchone()
    assert row["verify_mode"] == 1, "验证任务必须带 verify_mode=1（A2 冻结标记）"
    assert row["two_stage"] == 1 and row["status"] == "ready_for_review"


# ---------- 图级路由拦截 ----------

def test_after_evaluate_frozen_never_regenerates():
    from test_agent.graph.builder import _after_evaluate
    assert _after_evaluate({"verdict": "repair_gen", "verify_mode": 1}) == "report", \
        "冻结任务 repair_gen 不进 generate（不补题）"
    assert _after_evaluate({"verdict": "repair_env", "verify_mode": 1}) == "report", \
        "冻结任务环境失败也不重跑（如实进报告，不伪装继续）"
    assert _after_evaluate({"verdict": "failed", "verify_mode": 1}) == "report"


def test_after_evaluate_normal_routes_unchanged():
    """非 verify 任务逐字节回归锁：R-Loop 保留。"""
    from test_agent.graph.builder import _after_evaluate
    assert _after_evaluate({"verdict": "pass"}) == "report"
    assert _after_evaluate({"verdict": "stop"}) == "report"
    assert _after_evaluate({"verdict": "repair_env"}) == "provision"
    assert _after_evaluate({"verdict": "repair_gen"}) == "generate"
    assert _after_evaluate({"verdict": "failed"}) == "generate"


def test_after_review_frozen_never_reenters():
    from test_agent.graph.builder import _after_review
    assert _after_review({"verdict": "repair_gen", "verify_mode": 1}) == "__end__", \
        "冻结任务 review 回炉请求也必须终止（同卷比较前提）"
    assert _after_review({"verdict": "pass", "verify_mode": 1}) == "__end__"


def test_after_review_normal_routes_unchanged():
    from test_agent.graph.builder import _after_review
    assert _after_review({"verdict": "repair_gen"}) == "generate"
    assert _after_review({"verdict": "pass"}) == "__end__"


# ---------- run_task_exec：verify_mode 进 state + 指纹审计留痕 ----------

class _FakeGraph:
    def __init__(self, captured):
        self._captured = captured

    def invoke(self, init, cfg):
        self._captured.update(init)
        return {"done": True, "verdict": "pass"}


def _run_exec(tmp_path, monkeypatch, conn, tid):
    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    ws.mkdir(parents=True, exist_ok=True)
    from test_agent.graph import builder
    captured = {}
    monkeypatch.setattr(builder, "build_exec_graph",
                        lambda deps: _FakeGraph(captured))
    monkeypatch.setattr(builder, "make_deps",
                        lambda: types.SimpleNamespace(
                            conn=conn, runner=types.SimpleNamespace(cleanup=lambda n: None)))
    ret = builder.run_task_exec(tid)
    return ret, captured


def test_run_task_exec_freeze_audit_trail(tmp_path, monkeypatch):
    conn, tid, _, _ = _make_task(tmp_path, monkeypatch)
    sid = conn.execute("INSERT INTO test_scripts(task_id, framework, filename, content, content_hash)"
                       " VALUES(?, 'pytest', 't.py', 'def t(): pass', 'h0')", (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id, source, name, content, enabled) "
                 "VALUES(?, 'ai', 'c1', 'assert 1', 1)", (sid,))
    conn.commit()
    ret, captured = _run_exec(tmp_path, monkeypatch, conn, tid)
    assert captured.get("verify_mode") == 1, "verify_mode 必须进 TaskState（图内条件边可见）"
    msgs = [r["content"] for r in conn.execute(
        "SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id=m.session_id "
        "WHERE s.task_id=? ORDER BY m.id", (tid,))]
    freeze = [m for m in msgs if m.startswith("[verify-freeze]")]
    # 0-A（2026-11-20）扩约：基线指纹 + 终态指纹 + 分文件对账 三条留痕
    # （本测试任务行直插、未经 create_verify_task → 无快照 → 对账如实报 missing_snapshot）
    assert len(freeze) == 3, f"基线+终态指纹+分文件对账三条留痕，实得 {len(freeze)}"
    assert "一致：用例集全程未被改动" in freeze[1], "mock 图未改用例 → 终态指纹必须一致"
    assert "冻结快照缺失" in freeze[2], "无快照任务的对账必须如实上报而非伪装一致"


def test_run_task_exec_normal_task_no_freeze_noise(tmp_path, monkeypatch):
    conn, tid, _, _ = _make_task(tmp_path, monkeypatch, verify_mode=0,
                                 requirement="普通需求")
    ret, captured = _run_exec(tmp_path, monkeypatch, conn, tid)
    assert not captured.get("verify_mode"), "普通任务不注入冻结态"
    msgs = [r["content"] for r in conn.execute(
        "SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id=m.session_id "
        "WHERE s.task_id=? ORDER BY m.id", (tid,))]
    assert not [m for m in msgs if m.startswith("[verify-freeze]")], \
        "普通任务零冻结留痕（零行为变化）"


# ---------- 指纹函数 ----------

def test_case_set_fingerprint_changes_when_case_mutated(tmp_path, monkeypatch):
    """指纹对内容/启用集敏感——审计告警的前提。"""
    conn, tid, _, _ = _make_task(tmp_path, monkeypatch, status="done", verify_mode=0,
                                 requirement="原需求")
    sid = conn.execute("INSERT INTO test_scripts(task_id, framework, filename, content, content_hash)"
                       " VALUES(?, 'pytest', 't.py', 'def t(): pass', 'h0')", (tid,)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id, source, name, content, enabled) "
                 "VALUES(?, 'ai', 'c1', 'assert 1', 1)", (sid,))
    conn.commit()
    from test_agent.skills.verify_fix import case_set_fingerprint
    fp0 = case_set_fingerprint(conn, tid)
    conn.execute("UPDATE test_cases SET enabled=0 WHERE name='c1'")
    conn.commit()
    assert case_set_fingerprint(conn, tid) != fp0, "启用集变更必须改变指纹"
