"""A1 放行门测试（2026-11-20 设计方案 docs/设计方案-文件级修复与验收设计-20261120.md 包 A）。

复现 Qoder 体检论断 1：_auto_review_loop 六个 fail-closed 分支（token 停车线/代审异常/
不可解析/增补失败/增补零条/轮次预算耗尽）全部停在 ready_for_review，与"收敛待执行"
终态同形；两个自动入口（post_prep_review_hook / run_task_auto）只看任务状态就放行
——审查失败仍自动入队执行，与 fail-closed 注释直接矛盾。

修复契约：_auto_review_loop 返回 {"outcome": "converged"|"fail_closed", "reason"}；
入口只凭 outcome 放行（状态检查保留为双保险）。
铁律：tmp 库、零真实 LLM、零容器。"""
import types

import pytest

from test_agent import config


def _make_task(tmp_path, monkeypatch, *, status="ready_for_review", ai_reviewer="auto",
               confirmed=1, two_stage=1):
    """tmp 库 + auto 任务，返回 (conn, task_id)。"""
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
        "ai_reviewer,two_stage) VALUES(?,?,0.7,?,?,?,?)",
        (pid, uid, status, confirmed, ai_reviewer, two_stage)).lastrowid
    conn.commit()
    return conn, tid


# 供 run_review_once mock 使用的两种 summary 形态
_REVIEW_CONVERGED = {"approved": 3, "deleted": 0, "regenerate": 0, "inserted": 0,
                     "feedback_texts": []}
_REVIEW_MORE = {"approved": 1, "deleted": 0, "regenerate": 0, "inserted": 0,
                "feedback_texts": ["补充边界用例"]}


def _patch_loop(monkeypatch, conn, *, budget=False, review_raises=False,
                review_returns=None, fb_raises=False, fb_generated=0, rounds=2):
    """patch _auto_review_loop 的全部外部依赖（函数内 from-import 取模块属性，
    patch 源模块即生效——test_ai_reviewer.py 同款）。"""
    from test_agent.graph import builder
    from test_agent.skills import ai_reviewer as ar
    monkeypatch.setattr(ar, "reviewer_llm", lambda: object())
    monkeypatch.setattr(ar, "token_budget_exceeded", lambda c, t: budget)
    if review_raises:
        def _boom(*a, **k):
            raise RuntimeError("代审调用失败（模拟）")
        monkeypatch.setattr(ar, "run_review_once", _boom)
    else:
        monkeypatch.setattr(ar, "run_review_once", lambda *a, **k: review_returns)

    def _fb(task_id, text, runner=None):
        if fb_raises:
            raise ValueError("增补失败（模拟）")
        return {"generated": fb_generated}
    monkeypatch.setattr(builder, "run_feedback_round", _fb)
    monkeypatch.setattr(builder, "_reviewer_msg", lambda *a, **k: None)
    monkeypatch.setattr(config, "AI_REVIEWER_MAX_ROUNDS", rounds)
    return types.SimpleNamespace(conn=conn, runner=None)


# ---------- 层 1：_auto_review_loop 返回值契约（六个 fail-closed 分支 + 收敛） ----------

def test_loop_returns_fail_closed_on_budget(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, budget=True)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "token 停车线必须报 fail_closed"


def test_loop_returns_fail_closed_on_exception(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_raises=True)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "代审异常必须报 fail_closed"


def test_loop_returns_fail_closed_on_unparsable(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_returns=None)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "代审输出不可解析必须报 fail_closed"


def test_loop_returns_fail_closed_on_feedback_error(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_returns=_REVIEW_MORE, fb_raises=True)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "增补失败必须报 fail_closed"


def test_loop_returns_fail_closed_on_zero_generated(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_returns=_REVIEW_MORE, fb_generated=0)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "增补 0 条必须报 fail_closed"


def test_loop_returns_fail_closed_on_rounds_exhausted(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_returns=_REVIEW_MORE,
                       fb_generated=2, rounds=2)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "fail_closed", "轮次预算耗尽必须报 fail_closed"


def test_loop_returns_converged_when_no_feedback(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    deps = _patch_loop(monkeypatch, conn, review_returns=_REVIEW_CONVERGED)
    result = builder._auto_review_loop(deps, tid)
    assert result["outcome"] == "converged", "代审收敛（无增补决策）必须报 converged"


# ---------- 层 2：入口 gate——只凭 outcome 放行 ----------

def _patch_no_docker(monkeypatch):
    """钩子 auto 分支构造 SandboxRunner 会探活 Docker daemon——单测零 Docker 纪律：
    patch 源模块构造器（函数内 from-import 取模块属性）。"""
    monkeypatch.setattr("test_agent.sandbox.runner.SandboxRunner",
                        lambda: types.SimpleNamespace())


def test_hook_fail_closed_never_enqueues(tmp_path, monkeypatch):
    """Qoder 复现场景回放：审查 fail-closed（日志已写'停在待审查交人工'），
    阶段一钩子不得自动入队。"""
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    _patch_no_docker(monkeypatch)
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "fail_closed", "reason": "预算耗尽"})
    enqueued = []
    monkeypatch.setattr("test_agent.store.queue.QueueWorker.enqueue",
                        classmethod(lambda cls, t, kind: enqueued.append(t)))
    builder.post_prep_review_hook(tid)
    assert enqueued == [], "fail-closed 后禁止自动入队（修复前：状态仍 ready_for_review 被放行）"


def test_hook_converged_enqueues_once(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    _patch_no_docker(monkeypatch)
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "converged", "reason": "收敛"})
    enqueued = []
    monkeypatch.setattr("test_agent.store.queue.QueueWorker.enqueue",
                        classmethod(lambda cls, t, kind: enqueued.append(t)))
    builder.post_prep_review_hook(tid)
    assert enqueued == [tid], "收敛后恰好入队一次"


def test_hook_converged_but_status_gone_still_holds(tmp_path, monkeypatch):
    """双保险：outcome 收敛但任务状态已被并发方改走（非 ready_for_review）→ 不入队。"""
    conn, tid = _make_task(tmp_path, monkeypatch, status="failed")
    from test_agent.graph import builder
    _patch_no_docker(monkeypatch)
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "converged", "reason": "收敛"})
    enqueued = []
    monkeypatch.setattr("test_agent.store.queue.QueueWorker.enqueue",
                        classmethod(lambda cls, t, kind: enqueued.append(t)))
    builder.post_prep_review_hook(tid)
    assert enqueued == [], "状态双保险保留：非 ready_for_review 不入队"


def test_run_task_auto_fail_closed_never_executes(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    monkeypatch.setattr(builder, "run_task_prep", lambda t: {})   # confirmed=1 会走真 prep——零 Docker 纪律
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "fail_closed", "reason": "不可解析"})
    executed = []
    monkeypatch.setattr(builder, "run_task_exec",
                        lambda t: executed.append(t) or {"task_id": t})
    deps = types.SimpleNamespace(conn=conn, runner=None)
    monkeypatch.setattr(builder, "make_deps", lambda: deps)
    ret = builder.run_task_auto(tid)
    assert executed == [], "fail-closed 后禁止自动执行（修复前：同形状态被放行）"
    assert ret.get("stopped") == "fail_closed"


def test_run_task_auto_converged_executes_once(tmp_path, monkeypatch):
    conn, tid = _make_task(tmp_path, monkeypatch)
    from test_agent.graph import builder
    monkeypatch.setattr(builder, "run_task_prep", lambda t: {})
    monkeypatch.setattr(builder, "_auto_review_loop",
                        lambda deps, t: {"outcome": "converged", "reason": "收敛"})
    executed = []
    monkeypatch.setattr(builder, "run_task_exec",
                        lambda t: executed.append(t) or {"task_id": t})
    deps = types.SimpleNamespace(conn=conn, runner=None)
    monkeypatch.setattr(builder, "make_deps", lambda: deps)
    builder.run_task_auto(tid)
    assert executed == [tid], "收敛后恰好执行一次"
