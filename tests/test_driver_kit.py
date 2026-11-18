# -*- coding: utf-8 -*-
"""驱动器公共层 v2 测试锁（2026-11-19 遗憾清偿件 5）。

事故回放：bugverify 首拍 /run 六格 409（env_gate）被当成功——本层强制
409 分流+点火自检。409 分流纯函数化，措辞改动=测试红（对齐 server.py）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data" / "reports" / "e10"))

from driver_kit import create_and_run, decide_409, ensure_server


def test_409_env_gate_is_retryable():
    """env_gate 409（环境未就绪）= 可重试——事故一的根因形态。"""
    assert decide_409("运行环境未就绪（Docker），已自动开始补齐；进度见环境状态栏") == "env_gate"


def test_409_already_queued_is_idempotent():
    assert decide_409("任务已放行，正在队列中等待执行（前序任务完成后自动开跑）") == "already_queued"
    assert decide_409("任务正在执行中") == "already_queued"


def test_409_fatal_cases():
    assert decide_409("任务未确认（fail-closed 确认门），禁止下发") == "fatal"
    assert decide_409("D1 准入代审未通过（…）") == "fatal"
    assert decide_409("") == "fatal"                      # 空 detail=未知 409，宁可上抛


def test_create_and_run_retries_env_gate(monkeypatch):
    """回放事故：run 首拍 env_gate 409 → 等待重试 → 二拍 200 成功（不再是假排队）。"""
    import driver_kit as dk
    calls = {"run": 0, "confirm": 0, "create": 0}

    class FakeResp:
        def __init__(self, code, detail=""):
            self.status_code = code
            self._detail = detail
        def json(self):
            return {"detail": self._detail, "task_id": 42}
        text = ""

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            if url.endswith("/api/tasks"):
                calls["create"] += 1
                return FakeResp(200, "")          # 创建返回 {"task_id":42}
            if url.endswith("/confirm"):
                calls["confirm"] += 1
                return FakeResp(200, "")
            if url.endswith("/run"):
                calls["run"] += 1
                if calls["run"] == 1:
                    return FakeResp(409, "运行环境未就绪（Docker），已自动开始补齐")
                return FakeResp(200, "")
        def get(self, url, headers=None, timeout=None):
            return FakeResp(200, "")

    monkeypatch.setattr(dk.time, "sleep", lambda *_: None)   # 测试不等真实 20s
    out = dk.create_and_run(FakeSession(), {}, "http://x",
                            {"path": "p", "requirement": "r"})
    assert out["tid"] == 42 and calls["run"] == 2          # 重试了一次且成功
    assert calls["confirm"] == 1                           # confirm 只跑一次（200）


def test_create_and_run_raises_on_fatal_409():
    """fatal 409（如未确认）必须上抛——绝不静默当成功（事故一教训）。"""
    import driver_kit as dk

    class FatalSession:
        def post(self, url, headers=None, json=None, timeout=None):
            if url.endswith("/api/tasks"):
                return FakeResp200()
            return FakeResp(409, "任务未确认（fail-closed 确认门），禁止下发")
    class FakeResp200:
        status_code = 200
        def json(self):
            return {"task_id": 7}
    class FakeResp:
        def __init__(self, code, detail):
            self.status_code = code
            self._d = detail
        def json(self):
            return {"detail": self._d}
        text = ""

    with pytest.raises(RuntimeError, match="未确认"):
        dk.create_and_run(FatalSession(), {}, "http://x", {"path": "p", "requirement": "r"})


def test_ensure_server_raises_when_dead(monkeypatch):
    """server 死亡必须当场报错——绝不静默等待（事故二教训）。"""
    import driver_kit as dk
    monkeypatch.setattr(dk.time, "sleep", lambda *_: None)   # 不等待真实间隔
    with pytest.raises(RuntimeError, match="不可达"):
        dk.ensure_server("http://127.0.0.1:1", timeout_s=1)
