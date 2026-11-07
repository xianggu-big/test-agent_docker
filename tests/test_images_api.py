"""镜像管理动态视图 API（三源合并/degraded 降级）——负例优先。

/api/images 三源：registered（IMAGE_BUILD_CTX，buildable）∪ local（docker 实扫）∪
aux（AUX_IMAGES sidecar/recipe 池），按镜像名去重（registered > local > aux）；
docker 不可达 → 200+degraded 标志（不抛 500，页面仍可用两源）。
runner 以 FakeRunner 注入（monkeypatch server.get_runner）。
"""
import types

import pytest
from fastapi.testclient import TestClient

from test_agent import config
from test_agent.api import server


class FakeImages:
    """docker images SDK 替身：按 name→(size_mb, raise) 路由。"""

    def __init__(self, existing: dict[str, int], list_raise: Exception | None = None):
        self.existing = existing
        self.list_raise = list_raise

    def get(self, name):
        if name in self.existing:
            return types.SimpleNamespace(attrs={"Size": self.existing[name] * 1_000_000})
        raise RuntimeError("no such image")

    def list(self):
        if self.list_raise:
            raise self.list_raise
        out = []
        for name, size in self.existing.items():
            out.append(types.SimpleNamespace(attrs={"Size": size * 1_000_000},
                                             tags=[name]))
        return out


class FakeRunner:
    def __init__(self, images: FakeImages):
        self.client = types.SimpleNamespace(images=images)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.store import db
    conn = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    conn.execute("INSERT INTO users(username,password_hash,role) VALUES('s','x','admin')")
    conn.commit()
    c = TestClient(server.app)
    # 鉴权走内存 TOKENS 表——直接注入登录态（不跑登录链路）
    monkeypatch.setitem(server.auth.TOKENS, "testtoken",
                        {"username": "s", "role": "admin", "id": 1})
    h = {"Authorization": "Bearer testtoken"}
    yield c, h, conn
    conn.close()


def _patch_runner(monkeypatch, images: FakeImages):
    monkeypatch.setattr(server, "get_runner", lambda: FakeRunner(images))


def test_three_source_merge_and_dedup(client, monkeypatch):
    """三源合并：registered 全在 + local 补充未注册镜像 + aux 标注；registered 优先。"""
    c, h, _ = client
    reg = next(iter(config.IMAGE_BUILD_CTX))                 # 任一注册镜像
    _patch_runner(monkeypatch, FakeImages({reg: 387, "mariadb:11": 458,
                                           "gcc:13": 1990}))
    r = c.get("/api/images", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["docker_scanned"] is True and "degraded" not in body
    by_name = {i["name"]: i for i in body["images"]}
    assert by_name[reg]["source"] == "registered" and by_name[reg]["buildable"] is True
    assert by_name[reg]["exists"] is True and by_name[reg]["size_mb"] == 387
    assert by_name["mariadb:11"]["source"] == "local"        # aux∩local → local 优先
    assert by_name["gcc:13"]["source"] == "local" and by_name["gcc:13"]["buildable"] is False
    aux = [i for i in body["images"] if i["source"] == "aux"]
    assert all(i["buildable"] is False for i in aux)
    assert any(i["name"] == "postgres:16" and i["exists"] is False for i in aux)


def test_degraded_when_docker_unreachable(client, monkeypatch):
    """负例：docker 守护进程不可达 → 200 + degraded 标志（不 500），注册/辅助两源仍在。"""
    c, h, _ = client
    _patch_runner(monkeypatch, FakeImages({}, list_raise=RuntimeError("daemon down")))
    r = c.get("/api/images", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["docker_scanned"] is False and "degraded" in body
    names = {i["name"] for i in body["images"]}
    assert set(config.IMAGE_BUILD_CTX) <= names              # 注册镜像仍逐个 exists 判定
    assert "mariadb:11" in names                             # aux 仍在
    for i in body["images"]:
        assert i["exists"] is False                          # 全部判缺失（get 同样失败）


def test_registered_missing_shows_buildable_false_exists(client, monkeypatch):
    """缺失注册镜像：exists=false 且 buildable=true（前端据此显示构建按钮）。"""
    c, h, _ = client
    _patch_runner(monkeypatch, FakeImages({}))               # 什么都不存在
    r = c.get("/api/images", headers=h)
    body = r.json()
    by_name = {i["name"]: i for i in body["images"]}
    for image in config.IMAGE_BUILD_CTX:
        assert by_name[image]["exists"] is False and by_name[image]["buildable"] is True


def test_build_endpoint_still_rejects_unregistered(client, monkeypatch):
    """构建端点契约不变：仅注册镜像可构建（local/aux 无按钮也不可调）。"""
    c, h, _ = client
    _patch_runner(monkeypatch, FakeImages({}))
    r = c.post("/api/images/gcc:13/build", headers=h)
    assert r.status_code == 404
