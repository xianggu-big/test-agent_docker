# -*- coding: utf-8 -*-
"""任务2（2026-11-22）：伪造网络——桩生命周期+旗标两态+fail-open。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeContainer2:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status
        self.id = f"id-{name}"
        self.removed = False
        self.started = False

    def start(self):
        self.started = True
        self.status = "running"

    def remove(self, force=False):
        self.removed = True


class FakeContainers2:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.run_kwargs = None

    def get(self, name):
        for c in self.existing:
            if c.name == name:
                return c
        import docker.errors
        raise docker.errors.NotFound(name)

    def run(self, image, **kw):
        self.run_kwargs = {"image": image, **kw}
        c = FakeContainer2(kw.get("name"))
        self.existing.append(c)
        return c


def test_flag_off_returns_none_no_docker_calls(tmp_path, monkeypatch):
    """旗标关=ensure_stub 直接 None，零容器操作（关态逐字节不变铁律）。"""
    import test_agent.config as config
    from test_agent.sandbox import fakenet
    monkeypatch.setattr(config, "FAKE_NET_ENABLED", False)
    fc = FakeContainers2()
    assert fakenet.ensure_stub(type("C", (), {"containers": fc})(), 123) is None
    assert fc.run_kwargs is None and not fc.existing


def test_flag_on_no_mappings_fail_open(tmp_path, monkeypatch):
    """旗标开但无桩定义目录=None（fail-open，不起桩）。"""
    import test_agent.config as config
    from test_agent.sandbox import fakenet
    monkeypatch.setattr(config, "FAKE_NET_ENABLED", True)
    monkeypatch.setattr(fakenet, "MAPPINGS_ROOT", tmp_path / "nonexist")
    fc = FakeContainers2()
    assert fakenet.ensure_stub(type("C", (), {"containers": fc})(), 123) is None
    assert fc.run_kwargs is None


def test_flag_on_with_mappings_starts_stub_on_internal_net(tmp_path, monkeypatch):
    """旗标开+mappings 在=起桩挂 FAKE_NET_NAME+bind mount mappings（只读）。"""
    import test_agent.config as config
    from test_agent.sandbox import fakenet
    monkeypatch.setattr(config, "FAKE_NET_ENABLED", True)
    monkeypatch.setattr(fakenet, "MAPPINGS_ROOT", tmp_path)
    mdir = tmp_path / "default" / "mappings"
    mdir.mkdir(parents=True)
    (mdir / "ep1.json").write_text('{"request": {"method": "GET", "url": "/x"}}',
                                   encoding="utf-8")
    fc = FakeContainers2()
    host = fakenet.ensure_stub(type("C", (), {"containers": fc})(), 777)
    assert host == "mta-stub-777:8080"
    assert fc.run_kwargs["network"] == config.FAKE_NET_NAME
    binds = fc.run_kwargs["volumes"]
    assert any("/home/wiremock/mappings" == v["bind"] and v["mode"] == "ro"
               for v in binds.values())


def test_running_stub_reused_not_duplicated(tmp_path, monkeypatch):
    """同名桩已 running=复用（幂等），不重复 run。"""
    import test_agent.config as config
    from test_agent.sandbox import fakenet
    monkeypatch.setattr(config, "FAKE_NET_ENABLED", True)
    monkeypatch.setattr(fakenet, "MAPPINGS_ROOT", tmp_path)
    mdir = tmp_path / "default" / "mappings"
    mdir.mkdir(parents=True)
    (mdir / "ep1.json").write_text('{}', encoding="utf-8")
    pre = FakeContainer2("mta-stub-888")
    fc = FakeContainers2([pre])
    host = fakenet.ensure_stub(type("C", (), {"containers": fc})(), 888)
    assert host == "mta-stub-888:8080"
    assert fc.run_kwargs is None, "running 桩不得重复创建"


def test_cleanup_stub_idempotent():
    """清桩幂等：存在删除，不在静默。"""
    from test_agent.sandbox import fakenet
    pre = FakeContainer2("mta-stub-999")
    fc = FakeContainers2([pre])
    fakenet.cleanup_stub(type("C", (), {"containers": fc})(), 999)
    assert pre.removed
    fakenet.cleanup_stub(type("C", (), {"containers": FakeContainers2()})(), 999)  # 不炸


def test_flag_default_off():
    import test_agent.config as config
    assert config.FAKE_NET_ENABLED is False
