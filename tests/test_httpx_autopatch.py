"""httpx 自动补装测试锁（2026-11-16 全语言 18 格实锤 task 1166）。

multi-module-demo 的 requirements 只声明 fastapi，漏 httpx——fastapi/starlette 的
TestClient 运行时必需，collect 闸 import 炸 → 三轮废案 → 同指纹熔断。
install_deps requirements 分支补装（环境适配，非改项目文件）。
"""
import json

from test_agent.skills.builtin import build_skills


class _R:
    def __init__(self, exit_code, stdout=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, ""


class _Runner:
    """按命令分流：requirements 装成功；import fastapi 过；import httpx 失败→补装。"""

    def __init__(self):
        self.installed = []

    @property
    def client(self):
        return self

    @property
    def containers(self):
        return self

    def get(self, name):
        return object()

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        if cmd[:2] == ["pip", "install"] and "-r" in cmd:
            return _R(0)
        if cmd[:2] == ["pip", "install"]:
            self.installed.append(cmd[-1])
            return _R(0)
        if cmd[:2] == ["python", "-c"] and "fastapi" in cmd[2]:
            return _R(0)
        if cmd[:2] == ["python", "-c"] and "httpx" in cmd[2]:
            return _R(1)                       # 漏声明 → 触发补装
        return _R(0)


def _deps_output(runner, manifest="requirements"):
    skills = build_skills(runner)
    return skills["install_deps"].func(1, {"container_name": "c",
                                           "deps_manifest": manifest})


def test_httpx_patched_when_fastapi_without_httpx():
    runner = _Runner()
    out = _deps_output(runner)
    assert runner.installed == ["httpx"], "fastapi 有而 httpx 缺 → 必须补装"
    assert "httpx 补装" in out


def test_httpx_not_installed_when_present():
    class _Full(_Runner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
            if cmd[:2] == ["pip", "install"] and "-r" not in cmd:
                raise AssertionError("httpx 已装不得补装")
            return _R(0)
    runner = _Full()
    out = _deps_output(runner)
    assert "httpx 补装" not in out


def test_httpx_patch_fail_open():
    class _Boom(_Runner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
            if cmd[:2] == ["python", "-c"]:
                raise RuntimeError("container died")
            return _R(0)
    out = _deps_output(_Boom())
    assert "requirements exit=0" in out, "探测炸不炸 requirements 主安装"
