# -*- coding: utf-8 -*-
"""LLM 模型 profile（任务卡-LLM模型选择-20261108）：
三档预设+custom 的校验/存储/运行时生效/key 永不入库/备胎互斥/make_chat 兜底。
坑预判 #2：apply 改写 config 是全局态——autouse fixture 必须还原，防测试顺序依赖。"""
import json
import sqlite3

import pytest

from test_agent import config
from test_agent.skills import llm_profile
from test_agent.skills.llm_profile import (_normalize, apply, apply_saved_if_any,
                                           current, resolve_key)


@pytest.fixture(autouse=True)
def _restore_config():
    """apply 改写 config.DEEPSEEK_* 是进程级全局态——每测后还原（坑预判 #2）。"""
    saved = (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL)
    yield
    config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL = saved


@pytest.fixture()
def db(tmp_path):
    """tmp 库（init_db(db_path) 不触发 apply_saved_if_any——测试不动全局态）。"""
    from test_agent.store.db import init_db
    p = tmp_path / "t.db"
    init_db(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------- 校验（非法拒） ----------

def test_非法provider拒():
    assert _normalize({"provider": "openai"}) is None
    assert _normalize({}) is None
    assert _normalize("deepseek") is None


def test_custom缺字段拒():
    assert _normalize({"provider": "custom", "base_url": "http://x", "model": "m"}) is None  # 缺 key_env
    assert _normalize({"provider": "custom", "model": "m", "key_env": "K"}) is None          # 缺 base_url
    assert _normalize({"provider": "custom", "base_url": "ftp://x", "model": "m",
                       "key_env": "K"}) is None                                              # 非 http/https
    assert _normalize({"provider": "custom", "base_url": "http://x", "model": "m",
                       "key_env": "bad name"}) is None                                       # 变量名形态
    ok = _normalize({"provider": "custom", "base_url": "https://x/v1", "model": "m",
                     "key_env": "MY_KEY"})
    assert ok and ok["provider"] == "custom"


def test_预设档model可改():
    out = _normalize({"provider": "deepseek", "model": "deepseek-reasoner"})
    assert out["model"] == "deepseek-reasoner"
    assert out["base_url"] == llm_profile.PRESETS["deepseek"]["base_url"]


# ---------- current / apply / 运行时生效 ----------

def test_无行默认deepseek(db):
    p = current(db)
    assert p["provider"] == "deepseek"
    assert p["base_url"] == config.DEEPSEEK_BASE_URL


def test_apply改写config运行时值(db):
    p = apply({"provider": "qwen"}, db, updated_by="tester")
    assert p["model"] == "qwen-plus"
    assert config.DEEPSEEK_BASE_URL == llm_profile.PRESETS["qwen"]["base_url"]
    assert config.DEEPSEEK_MODEL == "qwen-plus"
    assert p["updated_by"] == "tester" and p["updated_at"]


def test_key永不入库(db):
    # 恶意/误传带 key 本体的字段——入库 JSON 里不得出现
    apply({"provider": "deepseek", "key": "sk-secret123", "api_key": "sk-secret123"}, db)
    row = db.execute("SELECT value FROM app_settings WHERE key='llm_profile'").fetchone()
    assert row is not None
    assert "sk-secret123" not in row["value"]
    assert "key" not in json.loads(row["value"])       # 只有 key_env 名字，无 key 字段


def test_切换local(db):
    p = apply({"provider": "local"}, db)
    assert p["base_url"] == "http://localhost:11434/v1"
    assert config.DEEPSEEK_MODEL == "qwen3-4b-inst-test"


# ---------- resolve_key ----------

def test_resolve_key_local恒ollama():
    assert resolve_key({"provider": "local"}) == "ollama"


def test_resolve_key_env读取与缺失(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    p = llm_profile.PRESETS["qwen"]
    assert resolve_key(p) is None
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert resolve_key(p) == "sk-test"


# ---------- 启动恢复 ----------

def test_apply_saved_if_any恢复(tmp_path, monkeypatch):
    from test_agent.store.db import init_db
    p = tmp_path / "r.db"
    init_db(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    apply({"provider": "local"}, conn)
    # 模拟重启：config 回默认 → apply_saved_if_any 应恢复 local
    config.DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    config.DEEPSEEK_MODEL = "deepseek-chat"
    monkeypatch.setattr(llm_profile, "current", lambda conn=None: {
        **llm_profile.PRESETS["local"], "updated_by": "", "updated_at": ""})
    apply_saved_if_any()
    assert config.DEEPSEEK_MODEL == "qwen3-4b-inst-test"
    assert config.DEEPSEEK_BASE_URL == "http://localhost:11434/v1"
    conn.close()


# ---------- make_chat 兜底（任务卡验收：兜底取 profile 且显式传参仍优先） ----------

def test_make_chat兜底取运行时profile(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # 兜底真源=app_settings 表（current()）——674/675 实锤：内存 config 会与表漂移
    monkeypatch.setattr(llm_profile, "current", lambda conn=None: {
        **llm_profile.PRESETS["local"], "updated_by": "", "updated_at": ""})
    from test_agent.skills.llm_client import make_chat
    llm = make_chat(temperature=0.2)               # 三件套不传
    assert llm._inner.model_name == "qwen3-4b-inst-test"
    assert "localhost:11434" in str(llm._inner.openai_api_base)


def test_make_chat兜底读表真源_内存漂移免疫(monkeypatch):
    """674/675 根因回归锁定：表已切回 deepseek、进程内存 config 残留 qwen——
    兜底必须取表值（current()），不取被污染的内存 config.DEEPSEEK_*。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config.DEEPSEEK_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"   # 模拟内存污染
    config.DEEPSEEK_MODEL = "qwen-plus"
    # 2026-11-10 路由化：make_chat 兜底走 current_doc（表真源），mock 它为 deepseek 档
    monkeypatch.setattr(llm_profile, "current_doc", lambda conn=None: {
        "default": "deepseek",
        "profiles": {"deepseek": llm_profile.PRESETS["deepseek"]},
        "routing": {}})
    from test_agent.skills.llm_client import make_chat
    llm = make_chat(temperature=0.2)
    assert llm._inner.model_name == "deepseek-chat"                     # 表值，非内存污染值
    assert "dashscope" not in str(llm._inner.openai_api_base)


def test_make_chat显式传参仍优先(monkeypatch):
    from test_agent.skills.llm_client import make_chat
    monkeypatch.setattr(config, "DEEPSEEK_MODEL", "profile-model")   # 兜底值
    llm = make_chat(model="explicit-model", api_key="sk-x",
                    base_url="http://explicit.example/v1")
    assert llm._inner.model_name == "explicit-model"
    assert llm._inner.openai_api_key.get_secret_value() == "sk-x"
    assert "explicit.example" in str(llm._inner.openai_api_base)


def test_local时备胎跳过(monkeypatch):
    # 备胎互斥（§2.6）：当前 profile=local 时 _try_fallback 直接返回 None（同端点降级无意义）
    from test_agent.skills import llm_client
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(llm_profile, "current",
                        lambda conn=None: {**llm_profile.PRESETS["local"],
                                           "updated_by": "", "updated_at": ""})
    rc = llm_client.RobustChat(object(), phase="t")   # inner 不被触达（local 先短路）
    assert rc._try_fallback([("user", "hi")]) is None


def test_非local备胎行为不变(monkeypatch):
    # deepseek profile：不短路，走到构造备胎——构造抛异常→None→原 402 照抛（不依赖本机 ollama）
    from test_agent.skills import llm_client
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(llm_profile, "current",
                        lambda conn=None: {**llm_profile.PRESETS["deepseek"],
                                           "updated_by": "", "updated_at": ""})

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("构造失败（测试注入）")

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _Boom)
    rc = llm_client.RobustChat(object(), phase="t")
    assert rc._try_fallback([("user", "hi")]) is None


# ---------- API 权限（caller 403：staff=admin+developer，caller 双端点皆拒） ----------

def test_caller_403(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.api import server, auth
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        # 建 caller（非 admin/staff）并登录
        auth.register("prof_caller", "caller123")
        r2 = client.post("/api/login", json={"username": "prof_caller", "password": "caller123"})
        assert r2.status_code == 200
        h = {"Authorization": f"Bearer {r2.json()['token']}"}
        assert client.get("/api/llm-profile", headers=h).status_code == 403    # staff 端点
        assert client.post("/api/llm-profile", headers=h,
                           json={"provider": "local"}).status_code == 403      # admin 端点
        assert client.post("/api/llm-profile/test", headers=h,
                           json={"provider": "local"}).status_code == 403      # staff 端点
        # admin 三端点全通
        ha = {"Authorization": f"Bearer {r.json()['token']}"}
        assert client.get("/api/llm-profile", headers=ha).status_code == 200
        assert client.post("/api/llm-profile", headers=ha,
                           json={"provider": "local"}).status_code == 200
