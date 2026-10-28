"""环境自检与自动补齐（sandbox/preflight.py）的机制单测。

全部用假 Docker 客户端，不碰真 Docker、不真构建镜像。
"""
from pathlib import Path

import pytest

from test_agent import config
from test_agent.sandbox import preflight


class _FakeImage:
    attrs = {"Size": 120e6}


class _FakeImages:
    def __init__(self, existing=(), fail_build=False):
        self.existing = set(existing)
        self.fail_build = fail_build
        self.built = []

    def get(self, tag):
        if tag not in self.existing:
            raise LookupError(f"ImageNotFound: {tag}")
        return _FakeImage()

    def build(self, path=None, tag=None, rm=True):
        if self.fail_build:
            raise RuntimeError("build boom")
        self.built.append(tag)
        self.existing.add(tag)
        return None, []


class _FakeClient:
    def __init__(self, up=True, existing=(), fail_build=False):
        self.up = up
        self.images = _FakeImages(existing, fail_build)

    def ping(self):
        if not self.up:
            raise RuntimeError("daemon down")
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离工作区 + 只保留一个镜像，避免依赖真实环境。"""
    monkeypatch.setattr(preflight, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(config, "IMAGE_BUILD_CTX", {config.IMAGE_PY: tmp_path / "ctx"})
    (tmp_path / "ctx").mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    preflight.reset_client()
    yield tmp_path
    preflight.reset_client()


def _use(monkeypatch, client):
    monkeypatch.setattr(preflight, "get_client", lambda: client)


def test_probe_all_ready(env, monkeypatch):
    client = _FakeClient(up=True, existing=[config.IMAGE_PY])
    _use(monkeypatch, client)

    snap = preflight.probe()

    assert snap["ready"] is True
    assert {i["key"]: i["status"] for i in snap["items"]} == {
        "docker": "ok", f"image:{config.IMAGE_PY}": "ok",
        "workspace": "ok", "llm": "ok"}


def test_probe_docker_down_blocks(env, monkeypatch):
    _use(monkeypatch, None)

    snap = preflight.probe()

    assert snap["ready"] is False
    docker = next(i for i in snap["items"] if i["key"] == "docker")
    assert docker["status"] == "missing" and docker["blocking"] is True


def test_probe_without_llm_key_only_warns(env, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    _use(monkeypatch, _FakeClient(up=True, existing=[config.IMAGE_PY]))

    snap = preflight.probe()

    llm = next(i for i in snap["items"] if i["key"] == "llm")
    assert llm["status"] == "warn" and llm["blocking"] is False
    assert snap["ready"] is True      # Key 缺失不阻断主链路（诚实降级）


def test_ensure_starts_docker_and_reports_repaired(env, monkeypatch):
    """Docker 未起 → 自动拉起成功 → 记为 repaired 且最终就绪。"""
    state = {"client": None}
    calls = {"start": 0}

    def fake_get_client():
        return state["client"]

    def fake_start_docker(wait_s=0):
        calls["start"] += 1
        state["client"] = _FakeClient(up=True, existing=[config.IMAGE_PY])
        return True, "started"

    monkeypatch.setattr(preflight, "get_client", fake_get_client)
    monkeypatch.setattr(preflight, "start_docker", fake_start_docker)

    result = preflight.ensure(auto_repair=True)

    assert calls["start"] == 1
    assert "docker" in result["repaired"]
    assert result["ready"] is True


def test_ensure_missing_image_auto_builds(env, monkeypatch):
    """镜像缺失 → 自动构建 → 第二次 probe 通过。"""
    client = _FakeClient(up=True, existing=[])
    _use(monkeypatch, client)

    result = preflight.ensure(auto_repair=True)

    assert config.IMAGE_PY in client.images.built
    assert config.IMAGE_PY in result["repaired"]
    assert result["ready"] is True


def test_ensure_build_failure_marks_failed(env, monkeypatch):
    """构建失败 → 明确 failed，不谎称就绪。"""
    client = _FakeClient(up=True, existing=[], fail_build=True)
    _use(monkeypatch, client)

    result = preflight.ensure(auto_repair=True)

    assert config.IMAGE_PY in result["failed"]
    assert result["ready"] is False
    assert "环境未就绪" in preflight.summary(result)


def test_ensure_docker_start_failure_gives_actionable_message(env, monkeypatch):
    """Docker 拉不起来 → failed + 提示手动启动，不抛异常。"""
    _use(monkeypatch, None)
    monkeypatch.setattr(preflight, "start_docker",
                        lambda wait_s=0: (False, "请安装并手动启动 Docker Desktop"))

    result = preflight.ensure(auto_repair=True)

    assert "docker" in result["failed"]
    assert result["ready"] is False
    assert "Docker 守护进程" in preflight.summary(result)


def test_ensure_no_repair_mode_has_no_side_effects(env, monkeypatch):
    """auto_repair=False 只报告不动作。"""
    client = _FakeClient(up=True, existing=[])
    _use(monkeypatch, client)
    monkeypatch.setattr(preflight, "start_docker",
                        lambda wait_s=0: pytest.fail("不该尝试启动 Docker"))

    result = preflight.ensure(auto_repair=False)

    assert client.images.built == []
    assert result["repaired"] == []
    assert result["ready"] is False


# ---------- L1 下发守卫（api/server.env_gate） ----------

def test_env_gate_blocks_and_triggers_repair(monkeypatch):
    from fastapi import HTTPException
    from test_agent.api import server

    monkeypatch.setattr(server.preflight, "probe", lambda: {
        "ready": False,
        "items": [{"key": "docker", "label": "Docker 守护进程", "status": "missing",
                   "detail": "未连接", "blocking": True}]})
    triggered = {"n": 0}
    monkeypatch.setattr(server, "trigger_env_prepare",
                        lambda: triggered.__setitem__("n", triggered["n"] + 1) or {})

    with pytest.raises(HTTPException) as exc:
        server.env_gate()

    assert exc.value.status_code == 409
    assert "Docker 守护进程" in exc.value.detail
    assert triggered["n"] == 1          # 拒绝的同时必须自动开始补齐


def test_env_gate_passes_when_ready(monkeypatch):
    from test_agent.api import server

    monkeypatch.setattr(server.preflight, "probe", lambda: {"ready": True, "items": []})
    monkeypatch.setattr(server, "trigger_env_prepare",
                        lambda: pytest.fail("就绪时不该触发补齐"))

    server.env_gate()                   # 不抛即通过
