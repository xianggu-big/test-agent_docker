# -*- coding: utf-8 -*-
"""每节点 LLM 可替换（2026-11-10 任务卡 2 期）：多 profile 文档+phase 路由。

纪律锁：default 与 routing 全部指向同一 profile 时行为与单 profile 逐字节一致；
显式传参仍优先（逃生口）；老格式自动迁移幂等；未知 phase 组走 default 不误路由。
"""
import json
import sqlite3

import pytest

from test_agent import config
from test_agent.skills import llm_profile
from test_agent.skills.llm_client import make_chat
from test_agent.skills.llm_profile import (PHASE_GROUPS, apply, apply_routing,
                                           current, current_doc, load_doc,
                                           phase_group, resolve_phase)


@pytest.fixture(autouse=True)
def _restore_config():
    saved = (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL)
    yield
    config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL = saved


@pytest.fixture()
def db(tmp_path):
    from test_agent.store.db import init_db
    p = tmp_path / "t.db"
    init_db(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _seed_two_profiles(db, monkeypatch=None):
    """default=deepseek（deepseek-chat），另建 local profile。"""
    apply({"provider": "deepseek"}, db)
    apply({"provider": "local"}, db)          # default 指到 local
    apply({"provider": "deepseek"}, db)       # 再回 deepseek=default
    # 此时 profiles 应含 deepseek 与 local 两个 id
    doc = current_doc(db)
    assert set(doc["profiles"]) >= {"deepseek", "local"}
    if monkeypatch is not None:
        monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))


# ---------- 老格式迁移 ----------

def test_老格式迁移_幂等写回(db):
    legacy = {"provider": "qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
              "model": "qwen-plus", "key_env": "DASHSCOPE_API_KEY"}
    db.execute("INSERT INTO app_settings(key, value) VALUES('llm_profile', ?)",
               (json.dumps(legacy),))
    db.commit()
    doc = load_doc(db)                        # 读即迁移
    assert doc["default"] == "qwen"
    assert doc["profiles"]["qwen"]["model"] == "qwen-plus"
    assert doc["routing"] == {}
    row = json.loads(db.execute(
        "SELECT value FROM app_settings WHERE key='llm_profile'").fetchone()["value"])
    assert "profiles" in row and "default" in row     # 迁移后写回新格式
    doc2 = load_doc(db)                               # 幂等：再读不再变形
    assert doc2["default"] == doc["default"] and doc2["profiles"] == doc["profiles"]


# ---------- 路由命中 / 回退 / 逐字节一致 ----------

def test_routing命中_与回退_default(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local", "plan": "deepseek"}, db)
    assert resolve_phase("gen_tests", db)["model"] == "qwen3-4b-inst-test"   # generate 组命中 local
    assert resolve_phase("plan", db)["model"] == "deepseek-chat"             # plan 组指 deepseek
    assert resolve_phase("gen_report", db)["model"] == "deepseek-chat"       # 未列出→回退 default
    assert resolve_phase("definitely_new_phase", db)["model"] == "deepseek-chat"


def test_phase_group映射_grep实测口径():
    assert phase_group("gen_tests") == "generate"
    assert phase_group("gen_feedback") == "generate"
    assert phase_group("map_rtm") == "plan"
    assert phase_group("rewrite") == "repair"
    assert phase_group("ai_review_d2") == "review"
    # 2026-11-15 闲时找bug：make_chat 实际用的构造 phase（ai_reviewer.reviewer_llm）
    # 也必须命中 review 组——否则 D1/D2 代审恒走 default，review 组路由对代审不生效
    assert phase_group("ai_review") == "review"
    assert phase_group("ui_gen") == "ui_assert"
    assert phase_group("grpc_gen") == "grpc_assert"
    assert phase_group("L2.gen_requirement") == "l2"
    assert phase_group("l3_gen_scenarios") == "l3"
    assert phase_group("l4_gen_scenarios") == "l4"
    assert phase_group("definitely_new_phase") == "other"     # 未知→other 走 default
    assert len(PHASE_GROUPS) == 15   # 2026-11-21 包 3：+agent_loop；2026-11-27 B5：+module_profile（V5 坑预防——新 phase 显式落组）


def test_逐字节一致_routing空等于单profile(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    # routing 为空：resolve_phase 任何 phase 都必须等于 current()（default 档）
    for ph in ("gen_tests", "plan", "gen_report", "understand", ""):
        assert resolve_phase(ph, db)["model"] == current(db)["model"]
        assert resolve_phase(ph, db)["base_url"] == current(db)["base_url"]


def test_全指default_逐字节一致(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({g: "deepseek" for g in PHASE_GROUPS}, db)
    for ph in ("gen_tests", "plan", "rewrite"):
        assert resolve_phase(ph, db)["model"] == current(db)["model"]


# ---------- make_chat 路由（行为锁，不真调网络） ----------

def test_make_chat_phase路由生效(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    llm = make_chat(phase="gen_tests")                    # generate 组→local 档
    assert llm._inner.model_name == "qwen3-4b-inst-test"
    llm2 = make_chat(phase="plan")                        # 未配置组→default deepseek
    assert llm2._inner.model_name == "deepseek-chat"


def test_make_chat_显式传参仍优先(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    llm = make_chat(phase="gen_tests", model="explicit-model",
                    base_url="http://explicit/v1", api_key="sk-x")
    assert llm._inner.model_name == "explicit-model"
    assert llm._inner.openai_api_key.get_secret_value() == "sk-x"


def test_make_chat_未知phase回default(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    llm = make_chat(phase="multilang_gen_reqs")           # 未知组→other→default
    assert llm._inner.model_name == "deepseek-chat"


# ---------- apply_routing 校验 ----------

def test_apply_routing_未知组拒(db):
    _seed_two_profiles(db)
    with pytest.raises(ValueError, match="未知路由组"):
        apply_routing({"not_a_group": "deepseek"}, db)


def test_apply_routing_无效profile跳过_空串用default(db):
    _seed_two_profiles(db)
    doc = apply_routing({"generate": "no_such_profile", "plan": ""}, db)
    assert "generate" not in doc["routing"] and "plan" not in doc["routing"]

# ---------- 任务卡-B 补尾：ctx_budget 透传 + 备胎互斥按路由端点 ----------

def test_ctx_budget_透传给调用方(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")   # FA+KV 部署后的提档（研究文档 §1 L1）
    llm = make_chat(phase="gen_tests")
    assert getattr(llm, "_ctx_budget", None) == 16384      # 环境变量覆盖=部署事实优先


def test_ctx_budget_云端档无预算(monkeypatch):
    from test_agent.skills.llm_client import make_chat
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: {
        "default": "deepseek", "profiles": {"deepseek": llm_profile.PRESETS["deepseek"]},
        "routing": {}})
    llm = make_chat(phase="plan")
    assert getattr(llm, "_ctx_budget", None) is None       # 云端省略=不限


def test_备胎互斥_按路由端点判断(db, monkeypatch):
    # 任务卡-B 设计 5：generate 路由到 local 而 default=deepseek——旧"看 current 档位"
    # 判据会漏；升级后按 resolve_phase 的实际端点跳过
    from test_agent.skills import llm_client
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    rc = llm_client.RobustChat(object(), phase="gen_tests")   # 该 phase 路由到 local
    assert rc._try_fallback([("user", "hi")]) is None         # 互斥（不构造备胎）


def test_备胎互斥_云端phase不拦(db, monkeypatch):
    from test_agent.skills import llm_client
    _seed_two_profiles(db, monkeypatch)
    apply_routing({"generate": "local"}, db)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("x")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _Boom)
    rc = llm_client.RobustChat(object(), phase="plan")        # plan 走 deepseek（云端）
    assert rc._try_fallback([("user", "hi")]) is None         # 不短路→构造失败→None=原 402 照抛


# ---------- profile.timeout 下限语义（G 系统臂 718~720 实锤：写死 60/90s 必超时） ----------

def test_profile_timeout_本地档下限抬高(db, monkeypatch):
    _seed_two_profiles(db, monkeypatch)
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    llm = make_chat(phase="plan")             # default=deepseek：云端预设无 timeout 字段
    assert getattr(llm, "request_timeout", None) is None   # 云端行为不变
    llm2 = make_chat(phase="plan", timeout=60)             # 云端+写死 60：原样生效
    assert getattr(llm2, "request_timeout", None) == 60
    apply_routing({"plan": "local"}, db)
    llm3 = make_chat(phase="plan")                         # local：预设下限 600
    assert getattr(llm3, "request_timeout", None) == 600
    llm4 = make_chat(phase="plan", timeout=60)             # local+写死 60：被抬到 600
    assert getattr(llm4, "request_timeout", None) == 600
    llm5 = make_chat(phase="plan", timeout=900)            # 显式更大仍优先
    assert getattr(llm5, "request_timeout", None) == 900


# ---------- API 权限：llm-routing caller 403 ----------

def test_llm_routing_caller_403(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.api import server, auth
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        ha = {"Authorization": f"Bearer {r.json()['token']}"}
        auth.register("route_caller", "caller123")
        r2 = client.post("/api/login", json={"username": "route_caller", "password": "caller123"})
        hc = {"Authorization": f"Bearer {r2.json()['token']}"}
        assert client.get("/api/llm-routing", headers=hc).status_code == 403
        assert client.put("/api/llm-routing", headers=hc,
                          json={"routing": {}}).status_code == 403
        assert client.get("/api/llm-routing", headers=ha).status_code == 200


# ---------- 任务卡-H 衔接：老 profiles 条目缺字段按预设补齐（715 实锤） ----------

def test_老profiles条目缺ctx_budget读时补齐(db, monkeypatch):
    # ctx_budget 字段引入前存的 local 条目没有该键——load_doc 必须按 PRESETS 补齐，
    # 否则预算降采样永不触发（715：15295 tokens 发给 ctx=8192 的 qwen → 400）
    db.execute("INSERT INTO app_settings(key, value) VALUES('llm_profile', '{}') "
               "ON CONFLICT(key) DO NOTHING", ())
    legacy_doc = {"default": "local",
                  "profiles": {"local": {"provider": "local",
                                          "base_url": "http://localhost:11434/v1",
                                          "model": "qwen3-4b-inst-test", "key_env": ""}},
                  "routing": {}}
    db.execute("UPDATE app_settings SET value=? WHERE key='llm_profile'",
               (json.dumps(legacy_doc),))
    db.commit()
    doc = load_doc(db)
    assert doc["profiles"]["local"]["ctx_budget"] == 8192   # 默认=实测生效上限（2026-11-13 五组对照验证：server 层 8192 硬钳制，16k 虚标会致头部静默截断）
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: current_doc(db))
    from test_agent.skills.llm_client import make_chat
    llm = make_chat(phase="gen_tests")
    assert getattr(llm, "_ctx_budget", None) == 8192       # 预算到位 → 降采样可触发


# ---------- B2+ 报告页错配呈现（687 遗留：capability_mismatch 上 /api/report） ----------

def test_report_api_错配字段(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.api import server, auth
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        ha = {"Authorization": f"Bearer {client.post('/api/login', json={'username': 'admin', 'password': 'admin123'}).json()['token']}"}
        # 建任务+一条 [generate·需求-能力对齐警告] 消息（模拟 generate 轮留痕）
        t = client.post("/api/tasks", json={"repo_url": "r", "requirement": "x"},
                        headers=ha).json()
        tid = t["task_id"]
        c = server.get_conn()
        sess = c.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?,'test_agent')",
                         (tid,)).lastrowid
        c.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
                  (sess, 0, "assistant",
                   "[generate·需求-能力对齐警告] 需求提到登录注册但项目无账户体系。"))
        c.execute("INSERT INTO test_reports(task_id, passed, failed, coverage_line) "
                  "VALUES (?,0,0,0.0)", (tid,))
        c.commit()
        r = client.get(f"/api/tasks/{tid}/report", headers=ha).json()
        rep = r["reports"][0] if isinstance(r, dict) and "reports" in r else r[0]
        assert "需求提到登录注册" in rep["capability_mismatch"]
