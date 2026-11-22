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


# ---------- Loop-6（TODO-13）：repo_url 防御性校验 ----------

def test_clone_rejects_option_prefix_and_shell_metachars():
    """--upload-pack 形态/- 前缀/空白与 shell 元字符：直接 RuntimeError（不进 subprocess）。"""
    from test_agent.graph import nodes as N
    import pytest
    for bad in ("--upload-pack=touch /tmp/pwn https://x/y",
                "https://x/y.git --depth=2",
                "a b", "x;y", "a|b", "`id`", "$(id)"):
        with pytest.raises(RuntimeError, match="repo_url 非法"):
            N._validate_repo_url(bad)


def test_clone_accepts_legal_urls_and_local_paths():
    from test_agent.graph import nodes as N
    for good in ("https://github.com/a/b.git", "http://github.com/a/b",
                 "git@github.com:a/b.git", "ssh://git@host/a/b"):
        assert N._validate_repo_url(good) is None
    import tempfile, os
    d = tempfile.mkdtemp()
    assert N._validate_repo_url(d) is None  # 本地存在路径放行（os.path.isdir 分支）


# ---------- Loop-8：run 命令含收集错误容忍（三角色实验 30 轮熔断根因） ----------

def test_build_run_cmd_continues_on_collection_errors():
    from test_agent.graph.nodes import build_run_cmd
    cmd = build_run_cmd()
    assert "--continue-on-collection-errors" in cmd, "收集错误不得中断全局（tenacity 30 轮熔断根因）"
    assert "--cov=." in cmd and "-rfE" in cmd  # 其余语义不变


# ---------- Loop-9：R-Loop 起第 2 轮限定生成物文件（自带恒定失败不进补测循环） ----------

def test_build_run_cmd_round2_scopes_to_generated_files():
    from test_agent.graph.nodes import build_run_cmd
    cmd = build_run_cmd(round_no=2, gen_files=["test_gen_r1.py", "test_imported.py"])
    assert "test_gen_r1.py" in cmd and "test_imported.py" in cmd
    assert "--continue-on-collection-errors" in cmd  # Loop-8 语义保持


def test_build_run_cmd_round1_full_dir_baseline():
    from test_agent.graph.nodes import build_run_cmd
    cmd = build_run_cmd(round_no=1, gen_files=["test_gen_r1.py"])
    assert not any(f.endswith(".py") for f in cmd), "首轮保持全目录基线（自带测试）"


# ---------- 任务3（2026-11-22）：per-test 粒度超时——bench3 第四轮活案例 ----------
# find_first_in_sorted 格：1 条挂死用例烧穿整轮 90s 超时 → junit 未落盘 → 证据蒸发
# 判 not_detected（类①最强检测信号形态落不了账）。per-test 超时=唯一能把挂死信号
# 记进 junit 的机制：挂死那条 failed（Timeout 进 message），其余用例结果保住。

def test_build_run_cmd_pertest_timeout_off_by_default(monkeypatch):
    """关态逐字节不变：PER_TEST_TIMEOUT_S=0 时命令不得出现 --timeout（零变化铁律）。"""
    import test_agent.config as config
    from test_agent.graph.nodes import build_run_cmd
    monkeypatch.setattr(config, "PER_TEST_TIMEOUT_S", 0)
    cmd = build_run_cmd()
    assert "--timeout" not in cmd, "关态不得注入 --timeout"
    assert "--timeout-method" not in cmd


def test_build_run_cmd_pertest_timeout_on_injects_signal_method(monkeypatch):
    """开态：>0 时追加 --timeout N --timeout-method signal（真机容器实测反转：thread
    法超时即 os._exit 无 junit=证据蒸发复发；signal 法 junit 落盘才达验收标准）。"""
    import test_agent.config as config
    from test_agent.graph.nodes import build_run_cmd
    monkeypatch.setattr(config, "PER_TEST_TIMEOUT_S", 30)
    cmd = build_run_cmd()
    i = cmd.index("--timeout")
    assert cmd[i + 1] == "30"
    assert "--timeout-method" in cmd and cmd[cmd.index("--timeout-method") + 1] == "signal"


def test_build_run_cmd_pertest_timeout_round2_still_scopes(monkeypatch):
    """开态+round2：注入不破坏 Loop-9 生成物限定语义。"""
    import test_agent.config as config
    from test_agent.graph.nodes import build_run_cmd
    monkeypatch.setattr(config, "PER_TEST_TIMEOUT_S", 30)
    cmd = build_run_cmd(round_no=2, gen_files=["test_gen_r1.py"])
    assert "test_gen_r1.py" in cmd and "--timeout" in cmd


def test_build_run_cmd_js_unaffected_by_pertest_timeout(monkeypatch):
    """JS 侧第一版不做（jest 无原生 per-test timeout CLI——挂账），命令不受旗标影响。"""
    import test_agent.config as config
    from test_agent.graph.nodes import build_run_cmd_js
    monkeypatch.setattr(config, "PER_TEST_TIMEOUT_S", 30)
    cmd = build_run_cmd_js(1)
    assert "--timeout" not in cmd

