"""0-B registry 执行闸测试（2026-11-20 设计稿 docs/设计方案-AgentLoop文件级实施设计-20261120.md §2.2）。

对码实锤（2026-11-20）：registry.invoke 只有确认门+超时——arg_schema 落库但从不校验、
skills.enabled 列存在但从不查询。0-B 前置闸（顺序=拒绝优先级）：
1) enabled 闸：enabled=0 ⇒ denied；未注册（无行）放行=现状；
2) schema 闸：**双模式**——
   - JSON Schema 形态（值是 dict）=严格模式：required/enum/min-max/maxLength/
     additionalProperties=False 未知键拒（Agent Loop 新能力用）；
   - 传统简写形态（值是 str，如 {"cmd": "list[str]"}）=类型提示模式：只查已声明键
     非 None 时的类型，未声明键放行（存量兼容实锤：install_deps 的 has_requirements
     老布尔形态、deps_manifest=None 传参——严格模式会炸现有链路）；空 schema 跳过；
3) 风险分层：risk_level="high" 隐含确认门（与显式 requires_confirmation 融合；
   现网唯一 high=run_tests 本就显式声明=零行为变化）。
铁律：tmp 库、零容器、零 LLM。"""
import types

import pytest

from test_agent import config
from test_agent.skills.registry import Skill, SkillRegistry

STRICT_SCHEMA = {"type": "object",
                 "properties": {"mode": {"type": "string",
                                         "enum": ["read", "list", "grep"]},
                                "n": {"type": "integer", "minimum": 1, "maximum": 200}},
                 "required": ["mode"], "additionalProperties": False}


def _make_registry(tmp_path, monkeypatch, *, confirmed=1):
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
        "requirement_text) VALUES(?,?,0.7,'ready_for_review',?,'需求')",
        (pid, uid, confirmed)).lastrowid
    conn.commit()
    return SkillRegistry(conn), conn, tid


def _ok_skill(name="t_skill", *, risk="low", confirm=False, schema=None):
    return Skill(name=name, description="测试技能",
                 func=lambda task_id, args: "ok", risk_level=risk,
                 requires_confirmation=confirm,
                 arg_schema=schema if schema is not None else {})


def test_enabled_gate_denies(tmp_path, monkeypatch):
    """enabled=0 ⇒ denied（列存在但历史从不查询的对码补闸）。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill()
    reg.register(s)
    conn.execute("UPDATE skills SET enabled=0 WHERE name='t_skill'")
    conn.commit()
    r = reg.invoke(tid, s, {"x": 1})
    assert r.status == "denied" and "停用" in r.error


def test_strict_missing_required(tmp_path, monkeypatch):
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema=STRICT_SCHEMA)
    r = reg.invoke(tid, s, {})
    assert r.status == "denied" and "mode" in r.error


def test_strict_unknown_key_rejected(tmp_path, monkeypatch):
    """严格模式未知键拒——防模型经 args 注入内部字段。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema=STRICT_SCHEMA)
    r = reg.invoke(tid, s, {"mode": "read", "api_key": "steal"})
    assert r.status == "denied" and "api_key" in r.error


def test_strict_type_and_range(tmp_path, monkeypatch):
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema=STRICT_SCHEMA)
    assert reg.invoke(tid, s, {"mode": "read", "n": "x"}).status == "denied"
    assert reg.invoke(tid, s, {"mode": "read", "n": 0}).status == "denied"   # minimum 1
    assert reg.invoke(tid, s, {"mode": "read", "n": 500}).status == "denied"  # maximum 200
    assert reg.invoke(tid, s, {"mode": "read", "n": 5}).status == "ok"


def test_strict_enum_violation(tmp_path, monkeypatch):
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema=STRICT_SCHEMA)
    r = reg.invoke(tid, s, {"mode": "write"})
    assert r.status == "denied" and "枚举" in r.error


def test_legacy_hint_mode_compat(tmp_path, monkeypatch):
    """传统简写形态：未声明键放行（install_deps has_requirements 老形态兼容）。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema={"container_name": "str", "deps_manifest": "str"})
    r = reg.invoke(tid, s, {"container_name": "c", "deps_manifest": None,
                            "has_requirements": True})
    assert r.status == "ok", f"存量形态必须放行：{r.error}"


def test_legacy_type_mismatch_denied(tmp_path, monkeypatch):
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema={"n": "int"})
    r = reg.invoke(tid, s, {"n": "not-a-number"})
    assert r.status == "denied" and "int" in r.error


def test_empty_schema_skips(tmp_path, monkeypatch):
    """空 schema=存量兼容跳过（任意 args 放行）。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    s = _ok_skill(schema={})
    assert reg.invoke(tid, s, {"任意": ["args"]}).status == "ok"


def test_high_risk_implies_confirmation(tmp_path, monkeypatch):
    """0-B 风险分层：high 未显式声明确认门也拦（confirmed=0 → denied）。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch, confirmed=0)
    s = _ok_skill(risk="high")
    r = reg.invoke(tid, s, {})
    assert r.status == "denied" and "确认门" in r.error


def test_high_risk_confirmed_passes(tmp_path, monkeypatch):
    reg, conn, tid = _make_registry(tmp_path, monkeypatch, confirmed=1)
    s = _ok_skill(risk="high")
    assert reg.invoke(tid, s, {}).status == "ok"


def test_low_risk_unconfirmed_still_runs(tmp_path, monkeypatch):
    """现状锁：low/medium 不受影响（确认门只看 requires_confirmation）。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch, confirmed=0)
    s = _ok_skill(risk="low")
    assert reg.invoke(tid, s, {}).status == "ok"


def test_explicit_confirmation_still_works(tmp_path, monkeypatch):
    """现状锁：显式 requires_confirmation 行为不变。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch, confirmed=0)
    s = _ok_skill(confirm=True)
    assert reg.invoke(tid, s, {}).status == "denied"
