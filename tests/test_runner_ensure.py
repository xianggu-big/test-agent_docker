# -*- coding: utf-8 -*-
"""Loop-2（TODO-02/D10）：ensure_container 对『存在但未运行』容器的处理——
start 后仍非 running 必须重建，不得返回死容器（E7 任务167 Docker exec 409 实锤）。"""
from test_agent.sandbox.runner import SandboxRunner


class FakeContainer:
    def __init__(self, name, status):
        self.name = name
        self.status = status
        self.started = False
        self.removed = False

    def start(self):
        self.started = True
        # 可注入"start 后仍死"的病态行为
        self.status = self._after_start

    def remove(self, force=False):
        self.removed = True


class FakeContainers:
    def __init__(self, existing):
        self.existing = existing
        self.created = []

    def get(self, name):
        for c in self.existing:
            if c.name == name:
                return c
        raise self._not_found()

    def _not_found(self):
        from docker.errors import NotFound
        return NotFound("no such container")

    def run(self, image, name=None, **kw):
        c = FakeContainer(name, "running")
        self.created.append(c)
        self.existing.append(c)
        return c


class FakeClient:
    def __init__(self, existing):
        self.containers = FakeContainers(existing)


def _runner_with(existing, after_start):
    for c in existing:
        c._after_start = after_start
    r = SandboxRunner.__new__(SandboxRunner)  # 不触真实 Docker
    r.client = FakeClient(existing)
    return r


def test_start_recovers_running_container_is_reused():
    """start 后恢复 running：复用（保留依赖缓存的原语义）。"""
    c = FakeContainer("mta-run-1", "exited")
    r = _runner_with([c], after_start="running")
    out = r.ensure_container("img", "mta-run-1")
    assert out is c and c.started and not c.removed


def test_start_still_dead_container_is_rebuilt_not_returned():
    """start 后仍非 running（OOM/崩溃秒死）：必须销毁重建，不得返回死容器。"""
    c = FakeContainer("mta-run-1", "created")
    r = _runner_with([c], after_start="created")  # start 后依旧死
    out = r.ensure_container("img", "mta-run-1")
    assert c.removed, "死容器必须 force remove"
    assert out is not c and out.status == "running", "必须返回新建的 running 容器"


def test_missing_container_creates_fresh():
    """NotFound：直接新建（原语义）。"""
    r = _runner_with([], after_start="running")
    out = r.ensure_container("img", "mta-run-9")
    assert out.name == "mta-run-9" and out.status == "running"
