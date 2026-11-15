"""install_deps 依赖清单三类探测与分流安装（builtin.py）的机制单测。

全部用假 runner，不碰真 Docker、不真 pip 安装。
"""
from pathlib import Path

import pytest

from test_agent import config
from test_agent.graph.nodes import probe_deps_manifest as _probe_from_nodes
from test_agent.skills.builtin import build_skills, probe_deps_manifest


class _FakeExecResult:
    def __init__(self, exit_code=0, stdout="ok", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeContainers:
    def get(self, name):
        return object()  # install_deps 只透传给 exec_in，无需行为


class _FakeRunner:
    """记录 exec_in 命令序列的假沙箱执行器。"""

    def __init__(self):
        self.client = type("C", (), {"containers": _FakeContainers()})()
        self.calls = []  # [(cmd, timeout_s), ...]

    def exec_in(self, container, cmd, workdir="/work", timeout_s=120):
        self.calls.append((list(cmd), timeout_s))
        return _FakeExecResult()


def _install_deps(runner):
    return build_skills(runner)["install_deps"]


# ---------- 探测纯函数 ----------

def test_probe_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    assert probe_deps_manifest(tmp_path) == "requirements"


def test_probe_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert probe_deps_manifest(tmp_path) == "pyproject"


def test_probe_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert probe_deps_manifest(tmp_path) == "setup_py"


def test_probe_none(tmp_path):
    (tmp_path / "README.md").write_text("no manifest here\n")
    assert probe_deps_manifest(tmp_path) is None


def test_probe_priority_requirements_wins(tmp_path):
    """requirements + pyproject 并存 → 显式清单优先（最常见且最便宜）。"""
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert probe_deps_manifest(tmp_path) == "requirements"


def test_probe_priority_pyproject_over_setup(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert probe_deps_manifest(tmp_path) == "pyproject"


def test_probe_reexported_from_nodes():
    """provision 节点从 nodes 命名空间导入的是同一个探测函数（防两份漂移）。"""
    assert _probe_from_nodes is probe_deps_manifest


# ---------- install_deps 分流安装 ----------

def test_install_requirements_manifest(monkeypatch):
    monkeypatch.setattr(config, "PBT_ENABLED", False)
    r = _FakeRunner()
    out = _install_deps(r).func(1, {"container_name": "mta-run-1",
                                     "deps_manifest": "requirements"})
    # httpx 探测两连（1166 实锤：fastapi 有而 httpx 缺→补装）——fake 全过故无补装
    assert r.calls == [(["pip", "install", "-r", "requirements.txt"], 300),
                       (["python", "-c", "import fastapi"], 20),
                       (["python", "-c", "import httpx"], 20)]
    assert "requirements exit=0" in out


@pytest.mark.parametrize("manifest", ["pyproject", "setup_py"])
def test_install_project_manifests(monkeypatch, manifest):
    """pyproject / setup_py 都走 `pip install .`（装项目声明的依赖）。"""
    monkeypatch.setattr(config, "PBT_ENABLED", False)
    r = _FakeRunner()
    out = _install_deps(r).func(1, {"container_name": "mta-run-1",
                                     "deps_manifest": manifest})
    assert r.calls == [(["pip", "install", "."], 300)]
    assert f"{manifest} exit=0" in out


def test_install_no_manifest_honest_skip(monkeypatch):
    """无清单 → 零执行、诚实说明（不猜依赖）。"""
    monkeypatch.setattr(config, "PBT_ENABLED", False)
    r = _FakeRunner()
    out = _install_deps(r).func(1, {"container_name": "mta-run-1",
                                     "deps_manifest": None})
    assert r.calls == []
    assert "未发现依赖清单" in out


def test_install_legacy_has_requirements_compat(monkeypatch):
    """老调用方只传 has_requirements 布尔：True 等价 requirements（回退兼容）。"""
    monkeypatch.setattr(config, "PBT_ENABLED", False)
    r = _FakeRunner()
    _install_deps(r).func(1, {"container_name": "mta-run-1",
                              "has_requirements": True})
    # httpx 探测两连（1166 实锤：fastapi 有而 httpx 缺→补装）——fake 全过故无补装
    assert r.calls == [(["pip", "install", "-r", "requirements.txt"], 300),
                       (["python", "-c", "import fastapi"], 20),
                       (["python", "-c", "import httpx"], 20)]


def test_install_pbt_appends_hypothesis(monkeypatch):
    """PBT 开关开启时 hypothesis 追加安装（清单分流之外的第二段不变）。"""
    monkeypatch.setattr(config, "PBT_ENABLED", True)
    r = _FakeRunner()
    _install_deps(r).func(1, {"container_name": "mta-run-1",
                              "deps_manifest": "requirements"})
    assert r.calls[-1] == (["pip", "install", "hypothesis"], 300)


def test_install_manifest_failure_reported_honestly(monkeypatch):
    """pip install . 构建失败 → exit_code 原样上报（诚实降级，不吞错）。"""
    monkeypatch.setattr(config, "PBT_ENABLED", False)

    class _FailRunner(_FakeRunner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=120):
            self.calls.append((list(cmd), timeout_s))
            return _FakeExecResult(exit_code=1, stdout="", stderr="build boom")

    out = _install_deps(_FailRunner()).func(1, {"container_name": "mta-run-1",
                                                "deps_manifest": "pyproject"})
    assert "pyproject exit=1" in out
    assert "build boom" in out
