"""D 安全边界测试（2026-11-20 设计方案 docs/设计方案-文件级修复与验收设计-20261120.md 包 D）。

D1 连通性测试凭据外传：修复前 developer 可提交任意 base_url+key_env，后端读宿主
环境变量值发给该地址（staff 可调 test 但不能登记 profile——apply 是 admin 专属，
白名单=已登记 profile+预设端点 后通路关闭）。
D2 跨任务可写构建缓存：trusted=False 模式写缓存按任务隔离（缓存投毒免疫）；默认
trusted=True 行为与历史逐字节一致（当前语料全部可信）。
D3 UNC/设备路径：纯语法分类先行，isdir/resolve 触碰文件系统之前拒绝（Windows UNC
路径解析会发起网络认证访问）。
零真实网络、零真实 Docker、零真实凭据。"""
import types

import pytest

from test_agent import config


# ---------- D1：连通性测试白名单 ----------

def test_d1_test_connection_rejects_unlisted_base_url(monkeypatch):
    """白名单外的 base_url 直接拒绝——不发请求、不解析 key（凭据外传通路关闭）。"""
    from test_agent.skills import llm_profile as lp
    called = {}

    def _no_chat(p, timeout_s=60):
        called["invoked"] = True
        raise AssertionError("白名单外的请求不应发起任何网络调用")
    monkeypatch.setattr(lp, "make_test_chat", _no_chat)
    monkeypatch.setattr(lp, "resolve_key", lambda p: "SHOULD_NOT_READ")
    ret = lp.test_connection(
        {"provider": "custom", "base_url": "https://evil.example.com/v1",
         "model": "x", "key_env": "DEEPSEEK_API_KEY"},
        allowed_base_urls={"https://api.deepseek.com"})
    assert ret["ok"] is False and "白名单" in ret["error"]
    assert "invoked" not in called, "拒绝路径零网络调用"
    assert "SHOULD_NOT_READ" not in str(ret), "拒绝路径不回显凭据"


def test_d1_test_connection_allows_listed(monkeypatch):
    from test_agent.skills import llm_profile as lp

    class _FakeChat:
        def invoke(self, msgs):
            return types.SimpleNamespace(content='{"ok": true}')
    monkeypatch.setattr(lp, "make_test_chat", lambda p, t=60: _FakeChat())
    monkeypatch.setattr(lp, "resolve_key", lambda p: "k")
    ret = lp.test_connection(
        {"provider": "custom", "base_url": "https://api.deepseek.com",
         "model": "deepseek-chat", "key_env": "K"},
        allowed_base_urls={"https://api.deepseek.com"})
    assert ret["ok"] is True


def test_d1_known_base_urls_includes_presets_and_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    from test_agent.skills import llm_profile as lp
    conn = db.get_conn()
    urls = lp.known_base_urls(conn)
    assert any("deepseek" in u for u in urls), "预设端点必须在白名单"
    assert any("localhost:11434" in u or "11434" in u for u in urls), \
        "本地 Ollama 端点必须在白名单"


def test_d1_make_test_chat_locks_redirects_off():
    """follow_redirects=False 显式锁死（防升级漂移——重定向会把凭据带到 3xx 目标）。"""
    import inspect
    from test_agent.skills import llm_profile as lp
    src = inspect.getsource(lp.make_test_chat)
    assert "follow_redirects=False" in src, "重定向必须显式关闭（grep 锁）"


# ---------- D2：构建缓存按任务隔离 ----------

def test_d2_trusted_mode_unchanged(tmp_path, monkeypatch):
    """trusted=True（默认）：路径与历史逐字节一致（跨任务共享）。"""
    monkeypatch.setattr(config, "BUILD_CACHE_HOST_DIR", tmp_path / "cache")
    from test_agent.graph.nodes import cache_binds_for
    binds = cache_binds_for(config.IMAGE_JAVA, task_id=7, trusted=True)
    host = str(tmp_path / "cache" / "m2")
    assert list(binds.keys()) == [host], "可信模式路径不含 task 段（零行为变化）"


def test_d2_untrusted_mode_isolates_per_task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BUILD_CACHE_HOST_DIR", tmp_path / "cache")
    from test_agent.graph.nodes import cache_binds_for
    b1 = cache_binds_for(config.IMAGE_JAVA, task_id=7, trusted=False)
    b2 = cache_binds_for(config.IMAGE_JAVA, task_id=8, trusted=False)
    host7 = str(tmp_path / "cache" / "m2" / "task_7")
    assert list(b1.keys()) == [host7], "不可信模式写缓存按任务隔离"
    assert list(b2.keys()) == [str(tmp_path / "cache" / "m2" / "task_8")], \
        "任务 8 的缓存与任务 7 零共享（缓存投毒免疫）"
    b3 = cache_binds_for(config.IMAGE_JAVA, task_id=7, trusted=False)
    assert list(b3.keys()) == [host7], "同任务回炉复用同缓存目录"


def test_d2_non_cache_image_returns_none():
    from test_agent.graph.nodes import cache_binds_for
    assert cache_binds_for("mta-test:py312", task_id=1, trusted=False) is None


def test_d2_config_flag_default_trusted():
    assert config.BUILD_CACHE_TRUSTED is True, "默认可信模式=现网行为逐字节不变"


# ---------- D3：UNC/设备路径拒绝 ----------

def test_d3_unc_backslash_rejected():
    from test_agent.store.projects import normalize_repo_key
    with pytest.raises(ValueError, match="UNC"):
        normalize_repo_key("\\\\server\\share\\repo")


def test_d3_unc_forward_slash_rejected():
    from test_agent.store.projects import normalize_repo_key
    with pytest.raises(ValueError, match="UNC"):
        normalize_repo_key("//server/share/repo")


def test_d3_device_path_rejected():
    from test_agent.store.projects import normalize_repo_key
    with pytest.raises(ValueError, match="UNC"):
        normalize_repo_key("\\\\.\\PhysicalDrive0")


def test_d3_normal_paths_untouched(tmp_path):
    """本地目录/URL 语义不变（回归锁）。"""
    from test_agent.store.projects import normalize_repo_key
    k1 = normalize_repo_key(str(tmp_path))
    assert k1.startswith("local:")
    k2 = normalize_repo_key("https://github.com/owner/repo.git")
    assert k2.startswith("url:") and "github.com/owner/repo" in k2, "URL 归一行为不变"


def test_d3_api_rejects_unc_with_400(tmp_path, monkeypatch):
    """API 层：UNC 输入 → 400（原样 500 语义错）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    from fastapi import HTTPException
    from test_agent.api.server import api_create_task
    with pytest.raises(HTTPException) as ei:
        api_create_task(body={"repo_url": "\\\\evil\\share", "requirement": "x"},
                        user={"user_id": 1, "username": "t"})
    assert ei.value.status_code == 400
