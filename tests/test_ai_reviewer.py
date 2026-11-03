"""AI 代审（docs/AI代审设计-20261031.md）：结构化决策解析 / 静态闸校验（防 vibes 审查）/
证人保护红线 / fail-closed / hybrid 抽检落库 / token 停车线。
铁律：pytest 不触碰真实库——DB 用例全部走 tmp 库。"""
import json
from types import SimpleNamespace

import pytest

from test_agent import config
from test_agent.skills import ai_reviewer as ar
from test_agent.store import db


class FakeLLM:
    """fake-LLM：invoke 返回固定 content（带 usage_metadata=None → 不记台账，诚实不记 0）。"""

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=self.content, usage_metadata=None)


D1_OK = json.dumps({"decision": "approve",
                    "reasoning": "需求含可验证行为陈述，值得投入执行",
                    "evidence": {"risk_points": ["除零边界"]}}, ensure_ascii=False)
D2_OK = json.dumps({"decisions": [
    {"decision": "approve", "target": "test_a", "reasoning": "覆盖条件1的等价类有效类",
     "evidence": {"condition_ids": [1]}},
    {"decision": "reject", "target": "test_b", "reasoning": "与 test_a 重复覆盖条件1",
     "evidence": {"condition_ids": [1]}},
    {"decision": "regenerate", "target": "test_c", "reasoning": "裸真值断言无证伪能力",
     "feedback": "改为具体值比较", "evidence": {"condition_ids": [2]}},
    {"decision": "insert", "target": "缺失：除零异常路径", "reasoning": "条件2的无效类无用例",
     "feedback": "补一条除零 pytest.raises 用例", "evidence": {"condition_ids": [2]}},
]}, ensure_ascii=False)


def _seed_review_env(conn):
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed) "
        "VALUES (1, ?, '需求：验证 add 与 div', 1)", (uid,)).lastrowid
    conn.execute("INSERT INTO test_conditions(task_id, condition, priority) VALUES (?, '条件甲', '高')",
                 (tid,))
    conn.execute("INSERT INTO test_conditions(task_id, condition, priority) VALUES (?, '条件乙', '中')",
                 (tid,))
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, filename, content, content_hash)
           VALUES (?, 'pytest', 1, 'test_gen_r1.py', 'def test_a():\n    assert 1\n', 'h')""", (tid,))
    for name in ("test_a", "test_b", "test_c"):
        conn.execute(
            "INSERT INTO test_cases(script_id, source, name, enabled, content) "
            "VALUES (?, 'ai', ?, 1, ?)", (cur.lastrowid, name, f"def {name}():\n    assert 1\n"))
    conn.commit()
    return tid


# ---------- D1 ----------

def test_parse_d1_valid_and_failclosed():
    assert ar.parse_d1(D1_OK)["decision"] == "approve"
    assert ar.parse_d1('{"decision": "approve", "reasoning": ""}') is None      # 空理由=vibes
    assert ar.parse_d1('{"decision": "maybe", "reasoning": "x"}') is None       # 非法决策
    assert ar.parse_d1("不是 JSON") is None                                     # fail-closed
    assert ar.parse_d1('{"decision": "reject", "reasoning": "需求不可测"}')["decision"] == "reject"


# ---------- D2 解析 + 校验 ----------

def test_parse_review_decisions():
    ds = ar.parse_review_decisions(D2_OK)
    assert ds and len(ds) == 4 and ds[0]["decision"] == "approve"
    assert ar.parse_review_decisions('{"decisions": []}') is None               # 没审=fail-closed
    assert ar.parse_review_decisions('{"decisions": [{"decision": "approve"}]}') is None  # 缺 target/reasoning
    assert ar.parse_review_decisions("垃圾输出") is None


def test_validate_decisions_static_gates():
    ds = ar.parse_review_decisions(D2_OK)
    # 条件号 99 不存在 = 幻觉证据 → 剔除
    ds_bad = [dict(ds[0], evidence={"condition_ids": [99]})]
    kept, dropped = ar.validate_decisions(ds_bad, {1, 2}, {"test_a"}, set())
    assert kept == [] and "幻觉证据" in dropped[0]
    # reject 不存在的用例 → 剔除
    kept, _ = ar.validate_decisions([ds[1]], {1, 2}, {"test_a"}, set())
    assert kept == []
    # 全合法 → 全保留
    kept, dropped = ar.validate_decisions(ds, {1, 2}, {"test_a", "test_b", "test_c"}, set())
    assert len(kept) == 4 and dropped == []
    # 证人保护：AI 无权 reject/regenerate 证人
    kept, dropped = ar.validate_decisions([ds[1], ds[2]], {1, 2},
                                          {"test_a", "test_b", "test_c"}, {"test_b", "test_c"})
    assert kept == []
    assert any("证人保护" in d for d in dropped)


def test_witness_protected_names():
    lines = [
        "FAILED test_gen_r1.py::test_div_zero - AssertionError: assert 0",
        "FAILED tests/test_old.py::test_repo - AssertionError: x",      # 自带测试按文件名不算
        "FAILED test_gen_fb1.py::test_x[case1] - AssertionError: y",    # 参数化剥后缀
        "FAILED test_gen_r2.py::test_env - ConnectionError: z",          # 非断言失败不算
        "FAILED test_imported.py::test_manual - AssertionError: m",      # 人工用例也是生成物侧证人
    ]
    assert ar.witness_protected_names(lines) == {"test_div_zero", "test_x", "test_manual"}


# ---------- run_review_once（fake-LLM 全链） ----------

@pytest.fixture()
def review_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    tid = _seed_review_env(conn)
    yield conn, tid
    conn.close()


def test_run_review_once_applies_and_records(review_env):
    conn, tid = review_env
    summary = ar.run_review_once(FakeLLM(D2_OK), conn, tid, round_no=1)
    assert summary["approved"] == 1 and summary["deleted"] == 1
    assert summary["regenerate"] == 1 and summary["inserted"] == 1
    assert len(summary["feedback_texts"]) == 2
    # 删除落库：test_b 停用（保留审计记录）
    enabled = {r["name"] for r in conn.execute(
        "SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id=c.script_id "
        "WHERE s.task_id=? AND c.enabled=1", (tid,))}
    assert enabled == {"test_a", "test_c"}
    # 决策台账 4 行全落库（reviewer_role=ai_proxy）
    rows = conn.execute("SELECT decision FROM review_decisions WHERE task_id=?", (tid,)).fetchall()
    assert len(rows) == 4
    conn.close()


def test_run_review_once_witness_protection_end_to_end(review_env):
    """证人保护红线（AI代审 §4）：历史断言失败的生成用例，AI 决策 reject 被闸拦截。"""
    conn, tid = review_env
    conn.execute(
        "INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')", (tid,))
    sid = conn.execute("SELECT id FROM agent_sessions WHERE task_id=?", (tid,)).fetchone()["id"]
    conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?, 0, 'assistant', ?)",
                 (sid, "FAILED test_gen_r1.py::test_b - AssertionError: 重复用券仍 201"))
    conn.commit()
    decisions = json.dumps({"decisions": [
        {"decision": "reject", "target": "test_b", "reasoning": "疑似冗余",
         "evidence": {"condition_ids": [1]}}]}, ensure_ascii=False)
    summary = ar.run_review_once(FakeLLM(decisions), conn, tid, round_no=1)
    assert summary["deleted"] == 0 and any("证人保护" in d for d in summary["dropped"])
    enabled = {r["name"] for r in conn.execute(
        "SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id=c.script_id "
        "WHERE s.task_id=? AND c.enabled=1", (tid,))}
    assert "test_b" in enabled        # 证人活着


def test_run_review_once_fail_closed(review_env):
    conn, tid = review_env
    assert ar.run_review_once(FakeLLM("完全不可解析"), conn, tid) is None
    # 无用例可审：诚实返回空摘要（不炸）
    conn.execute("DELETE FROM test_cases")
    conn.commit()
    summary = ar.run_review_once(FakeLLM(D2_OK), conn, tid)
    assert summary["note"]


# ---------- hybrid 抽检 + 停车线 ----------

def test_spotcheck_sample_deterministic():
    ids = list(range(1, 21))
    s1 = ar.spotcheck_sample(7, ids)
    s2 = ar.spotcheck_sample(7, ids)
    assert s1 == s2 and len(s1) == 4 and all(i in ids for i in s1)   # 20% + 稳定
    assert ar.spotcheck_sample(8, [1]) == {1}                        # 至少 1 条
    assert ar.spotcheck_sample(8, []) == set()


def test_record_spotcheck_uphold_and_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id) VALUES (1, ?)", (uid,)).lastrowid
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, filename, content, content_hash)
           VALUES (?, 'pytest', 1, 'test_gen_r1.py', 'x', 'h')""", (tid,))
    conn.execute(
        "INSERT INTO test_cases(script_id, source, name, enabled, content) "
        "VALUES (?, 'ai', 'test_del', 1, 'def test_del():\n    assert 1\n')", (cur.lastrowid,))
    conn.execute(
        """INSERT INTO review_decisions(task_id, decision, target, reasoning, reviewer_role, round)
           VALUES (?, 'reject', 'test_del', '重复覆盖', 'ai_proxy', 1)""", (tid,))
    did = conn.execute("SELECT id FROM review_decisions").fetchone()["id"]
    conn.commit()

    # 推翻删除：人工决策落库（human）+ 用例恢复
    out = ar.record_spotcheck(conn, did, "override", "我认为不重复", uid)
    assert out["restored"] == "test_del"
    assert conn.execute("SELECT enabled FROM test_cases WHERE name='test_del'").fetchone()["enabled"] == 1
    rows = conn.execute("SELECT decision, reviewer_role FROM review_decisions ORDER BY id").fetchall()
    assert rows[-1]["reviewer_role"] == "human" and rows[-1]["decision"] == "approve"
    # 同意：追加同决策人工记录（认可 AI 原始判断；不回滚用例状态——人工意志已生效）
    out = ar.record_spotcheck(conn, did, "uphold", "", uid)
    assert out["decision"] == "reject"
    with pytest.raises(LookupError):
        ar.record_spotcheck(conn, 9999, "uphold", "", uid)
    with pytest.raises(ValueError):
        ar.record_spotcheck(conn, did, "maybe", "", uid)
    conn.close()


def test_record_spotcheck_idempotent_no_prefix_false_positive(tmp_path, monkeypatch):
    """幂等防重必须 json_extract 精确匹配（真机实锤：LIKE '%"spotcheck_of": 6%' 被
    65/64 前缀命中误判重复——字符串 LIKE 不可做数值匹配）。6 与 65 互不误伤。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id) VALUES (1, ?)", (uid,)).lastrowid
    # AI 决策 #6 与 #65（全局自增 id 跨任务同池；显式指定 id 复现前缀场景）
    for did in (6, 65):
        conn.execute(
            """INSERT INTO review_decisions(id, task_id, decision, target, reasoning, reviewer_role, round)
               VALUES (?, ?, 'approve', 't', 'r', 'ai_proxy', 1)""", (did, tid))
    conn.commit()
    out6 = ar.record_spotcheck(conn, 6, "uphold", "", uid)
    assert out6.get("duplicate") is None                       # 首次正常落库
    out6b = ar.record_spotcheck(conn, 6, "uphold", "", uid)
    assert out6b.get("duplicate") is True                      # 同条同结论 → 幂等
    out65 = ar.record_spotcheck(conn, 65, "uphold", "", uid)
    assert out65.get("duplicate") is None                      # 65 ≠ 6：前缀不得误判（真机坑）
    n = conn.execute("SELECT COUNT(*) AS n FROM review_decisions WHERE reviewer_role='human'").fetchone()["n"]
    assert n == 2                                              # 恰两行，无重复
    conn.close()


def test_token_budget_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?, 'r', 'n')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id) VALUES (1, ?)", (uid,)).lastrowid
    conn.execute("INSERT INTO llm_calls(task_id, phase, prompt_tokens, completion_tokens, total_tokens) "
                 "VALUES (?, 'ai_review_d2', 0, 0, ?)", (tid, config.AI_REVIEWER_TOKEN_BUDGET - 1))
    conn.commit()
    assert ar.token_budget_exceeded(conn, tid) is False
    conn.execute("UPDATE llm_calls SET total_tokens=total_tokens+1 WHERE task_id=?", (tid,))
    conn.commit()
    assert ar.token_budget_exceeded(conn, tid) is True
    conn.close()


def test_confirm_endpoint_runs_d1_for_auto(tmp_path, monkeypatch):
    """A3（必做项工作包）：auto 任务单任务确认走 D1——approve 置 confirmed_by=agent:reviewer-*，
    reject 409 且保持未确认；off 任务不触发 D1（行为不变）。"""
    import json as _json
    from types import SimpleNamespace
    from test_agent.skills import ai_reviewer as ar
    from test_agent.api import server
    import fastapi
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username, password_hash) VALUES ('u','x')").lastrowid
    conn.execute("INSERT INTO projects(user_id, repo_url, name, primary_language) VALUES (?, 'r', 'n', 'python')", (uid,))
    tid = conn.execute("INSERT INTO test_tasks(project_id, user_id, requirement_text, two_stage, ai_reviewer) "
                       "VALUES (1, ?, '需求', 1, 'auto')", (uid,)).lastrowid
    conn.commit()

    class FakeLLM:
        def __init__(self, decision): self.d = decision
        def invoke(self, messages):
            return SimpleNamespace(content=_json.dumps(
                {"decision": self.d, "reasoning": "需求可测", "evidence": {}}, ensure_ascii=False),
                usage_metadata=None)
    monkeypatch.setattr(ar, "reviewer_llm", lambda: None)
    import test_agent.api.server as srv

    # reject → 409 且保持未确认
    # server 内是 from ..skills.ai_reviewer import reviewer_llm, run_d1_review（函数内 import）
    # → monkeypatch ar.run_d1_review 即可（函数内 import 每次取模块属性）
    monkeypatch.setattr(ar, "run_d1_review", lambda *a, **k: {"decision": "reject", "reasoning": "不可测", "evidence": {}})
    monkeypatch.setattr(ar, "reviewer_llm", lambda: object())
    with pytest.raises(fastapi.HTTPException) as ei:
        server.api_confirm(tid, {"user_id": uid, "username": "u", "role": "admin"})
    assert ei.value.status_code == 409 and "不可测" in ei.value.detail
    assert conn.execute("SELECT confirmed FROM test_tasks WHERE id=?", (tid,)).fetchone()["confirmed"] == 0

    # approve → confirmed_by=agent:reviewer-* + D1 决策落库（走真实 run_d1_review + FakeLLM）
    monkeypatch.setattr(ar, "run_d1_review", ar.run_d1_review.__wrapped__ if hasattr(ar.run_d1_review, "__wrapped__") else None)
    # 恢复真实落库函数（reject 分支 patch 掉了）：直接引用模块原函数
    import importlib
    real_run_d1 = importlib.reload(ar).run_d1_review
    monkeypatch.setattr(ar, "run_d1_review", real_run_d1)
    class FakeLLM2:
        def invoke(self, messages):
            return SimpleNamespace(content=_json.dumps(
                {"decision": "approve", "reasoning": "值得测", "evidence": {}}, ensure_ascii=False),
                usage_metadata=None)
    monkeypatch.setattr(ar, "reviewer_llm", lambda: FakeLLM2())
    out = server.api_confirm(tid, {"user_id": uid, "username": "u", "role": "admin"})
    assert out["d1_by_ai"] is True
    row = conn.execute("SELECT confirmed_by FROM test_tasks WHERE id=?", (tid,)).fetchone()
    assert str(row["confirmed_by"]).startswith("agent:reviewer-")
    n = conn.execute("SELECT COUNT(*) AS n FROM review_decisions WHERE task_id=? AND decision='approve'", (tid,)).fetchone()["n"]
    assert n == 1

    # off 任务：不触发 D1（直接人工确认）
    tid2 = conn.execute("INSERT INTO test_tasks(project_id, user_id, two_stage, ai_reviewer) "
                        "VALUES (1, ?, 1, 'off')", (uid,)).lastrowid
    conn.commit()
    called = []
    monkeypatch.setattr(ar, "run_d1_review", lambda *a, **k: called.append(1) or {"decision": "approve", "reasoning": "x", "evidence": {}})
    server.api_confirm(tid2, {"user_id": uid, "username": "u", "role": "admin"})
    assert called == []
    conn.close()

