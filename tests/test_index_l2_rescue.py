# -*- coding: utf-8 -*-
"""L2 覆盖率闭环兜底测试锁（2026-11-18 设计方案-L1L2L3辅助急救层 §2.3，7 例）。

覆盖：ok/zero_coverage/not_routed/防死循环（_l2_rescued）/旗标关逐字节不变/
集成消息留痕/成本护栏（急救不超轮次预算）。
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config
from test_agent.skills.project_index import coverage_check

# pytest-cov term-missing 表（skip-covered 口径：0% 文件必在表内，全覆盖文件省略）
_ZERO_COV_STDOUT = (
    "========== test session starts ==========\n"
    "---------- coverage: platform win32 ----------\n"
    "Name              Stmts   Miss  Cover   Missing\n"
    "---------------------------------------------------\n"
    "user/service.py       10     10     0%   1-10\n"
    "order/repo.py          8      8     0%   1-8\n"
    "TOTAL                  18     18     0%\n"
    "1 passed\n")
_PART_COV_STDOUT = _ZERO_COV_STDOUT.replace(
    "user/service.py       10     10     0%   1-10",
    "user/service.py       10      3    70%   4-6")


def _eval_state(**kw):
    base = {"task_id": 1, "session_id": 1, "primary_language": "python",
            "last_exit": 0, "last_stdout": _PART_COV_STDOUT, "last_stderr": "",
            "round_no": 1, "fingerprints": [],
            "index_scope_paths": ["user/"], "index_closure": ["user"]}
    base.update(kw)
    return base


# ---------------- coverage_check 纯函数三态 ----------------

def test_l2_1_ok_when_scope_covered():
    """L2-1 范围内有 cover>0 → ok。"""
    state = _eval_state()
    per = [{"file": "user/service.py", "stmts": 10, "miss": 3, "cover": 70},
           {"file": "order/repo.py", "stmts": 8, "miss": 8, "cover": 0}]
    assert coverage_check(state, per, coverage_line=0.0)["verdict"] == "ok"


def test_l2_2_zero_coverage_when_all_zero():
    """L2-2 范围内全 0 覆盖 → zero_coverage（急救触发条件）。"""
    state = _eval_state()
    per = [{"file": "user/service.py", "stmts": 10, "miss": 10, "cover": 0}]
    out = coverage_check(state, per, coverage_line=0.0)
    assert out["verdict"] == "zero_coverage"


def test_l2_3_not_routed_and_no_data():
    """L2-3 非路由格 → not_routed；无覆盖数据 → no_data（不猜）。"""
    assert coverage_check({}, [], coverage_line=None)["verdict"] == "not_routed"
    routed = _eval_state()
    assert coverage_check(routed, [], coverage_line=None)["verdict"] == "no_data"
    # skip-covered 语义：范围内缺席 + TOTAL>0 = 已覆盖（0% 文件必在缺口表）
    assert coverage_check(routed, [{"file": "other/x.py", "cover": 40}],
                          coverage_line=0.4)["verdict"] == "ok"
    # 范围内缺席 + TOTAL=0 = 什么都没测到
    assert coverage_check(routed, [], coverage_line=0.0)["verdict"] == "zero_coverage"


# ---------------- evaluate 节点接线（tmp 库驱动） ----------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    from test_agent.graph import nodes as N
    from test_agent.store import db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    c.execute("INSERT INTO modules(project_id, path, language, loc, strategy) "
              "VALUES (?, '.', 'python', 10, 'full')", (pid,))
    c.commit()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "user").mkdir()
    (ws / "user" / "service.py").write_text("def login(u):\n    return True\n", encoding="utf-8")
    nodes = N.make_nodes(types.SimpleNamespace(conn=c, runner=None, registry=None, skills={}))
    yield c, tid, ses, nodes
    c.close()


def _messages(c, ses) -> list[str]:
    return [r["content"] for r in c.execute(
        "SELECT content FROM agent_messages WHERE session_id=? ORDER BY seq", (ses,))]


def test_l2_4_rescue_expand_then_fallback(env, monkeypatch):
    """L2-4 急救链：首轮零覆盖→扩圈；再零覆盖→放弃路由回退全库（防死循环）。"""
    monkeypatch.setattr(config, "INDEX_L2_RESCUE_ENABLED", True)
    c, tid, ses, nodes = env
    # 扩圈素材：user→order→billing 调用链（hops=2 比 state 里的闭包多出 billing）
    ws = Path(config.DATA_DIR) / "ws"
    (ws / "user" / "service.py").write_text(
        "from order.repo import create\n\n\ndef login(u):\n    return create(u)\n",
        encoding="utf-8")
    (ws / "order").mkdir()
    (ws / "order" / "repo.py").write_text(
        "from billing.charge import charge\n\n\ndef create(i):\n    return charge(i)\n",
        encoding="utf-8")
    (ws / "billing").mkdir()
    (ws / "billing" / "charge.py").write_text("def charge(i):\n    return i\n", encoding="utf-8")
    st = _eval_state(task_id=tid, session_id=ses, workspace=str(ws),
                     last_stdout=_ZERO_COV_STDOUT, index_closure=["user"])
    out = nodes["evaluate"](st)
    assert out["verdict"] == "repair_gen" and "L2 急救" in out["stop_reason"]
    assert out["index_l2_rescued"] is True
    assert "billing" in out["index_closure"], f"扩圈并入 2 跳邻居: {out['index_closure']}"
    assert "[index·L2]" in " ".join(_messages(c, ses))
    # 第二次：已扩圈过（rescued=True）仍零覆盖 → 回退全库（scope 清空）
    st2 = _eval_state(task_id=tid, session_id=ses, workspace=str(ws),
                      last_stdout=_ZERO_COV_STDOUT,
                      index_closure=["user", "order", "billing"], index_l2_rescued=True)
    out2 = nodes["evaluate"](st2)
    assert out2["verdict"] == "repair_gen" and "回退全库" in out2["stop_reason"]
    assert out2["index_scope_paths"] == [], "放弃路由=scope 清空"
    assert "急救失败诚实出口" in " ".join(_messages(c, ses))


def test_l2_5_flag_off_byte_identical(env, monkeypatch):
    """L2-5 旗标关：整段不执行（零覆盖也不急救，verdict 维持 decide 原判）。"""
    monkeypatch.setattr(config, "INDEX_L2_RESCUE_ENABLED", False)
    c, tid, ses, nodes = env
    st = _eval_state(task_id=tid, session_id=ses,
                     last_stdout=_ZERO_COV_STDOUT)
    out = nodes["evaluate"](st)
    assert out["verdict"] == "repair_gen"          # decide 原判（覆盖率 0 不达标→补测）
    assert "L2 急救" not in out["stop_reason"]      # 无急救改写
    assert not any("[index·L2]" in m for m in _messages(c, ses))


def test_l2_6_pass_verdict_not_rescued(env):
    """L2-6 达标（verdict=pass）不触发急救——急救只挂在 repair_gen 分支。"""
    c, tid, ses, nodes = env
    # 真实 skip-covered 形态：user 100% 全覆盖→被省略；表里只剩有缺口的 order
    ok_stdout = (
        "---------- coverage: platform win32 ----------\n"
        "Name              Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------\n"
        "order/repo.py          8      2    75%   3-4\n"
        "TOTAL                  18      2    89%\n"
        "1 passed\n")
    st = _eval_state(task_id=tid, session_id=ses, last_stdout=ok_stdout,
                     coverage_target=0.7)
    out = nodes["evaluate"](st)
    assert out["verdict"] == "pass"
    assert not any("[index·L2]" in m for m in _messages(c, ses))


def test_l2_7_round_budget_guard(env, monkeypatch):
    """L2-7 成本护栏：轮次预算耗尽（decide→stop）时急救不触发，轮数不超 MAX_REPAIR_ROUNDS。"""
    monkeypatch.setattr(config, "INDEX_L2_RESCUE_ENABLED", True)
    c, tid, ses, nodes = env
    st = _eval_state(task_id=tid, session_id=ses, last_stdout=_ZERO_COV_STDOUT,
                     round_no=config.MAX_REPAIR_ROUNDS)   # 预算耗尽轮
    out = nodes["evaluate"](st)
    assert out["verdict"] == "stop", "预算耗尽必须维持 stop（急救不得击穿）"
    assert not any("[index·L2]" in m for m in _messages(c, ses))
