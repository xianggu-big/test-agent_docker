"""项目知识索引+需求路由测试锁（2026-11-18 设计方案落地）。

覆盖：build_index 三清单（模块/符号/接口）/match_requirement 三态路由
（模块直中/别名中/未命中回退）/scope_paths 源码过滤/TaskState 声明锁。
"""
import json
import sys
from pathlib import Path

import pytest

from test_agent import config
from test_agent.skills.project_index import (build_index, ensure_index,
                                             match_requirement)


@pytest.fixture()
def ws(tmp_path):
    """tmp 双模块项目：user/（auth 相关）+ order/ + express 路由 + openapi。"""
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "user" / "service.py").write_text(
        "def login(username):\n    return True\n\n\nclass UserStore:\n    pass\n",
        encoding="utf-8")
    (tmp_path / "order").mkdir()
    (tmp_path / "order" / "repo.py").write_text(
        "def create_order(item):\n    return {'id': 1}\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "app.get('/api/users', handler);\napp.post('/api/orders', h2);\n",
        encoding="utf-8")
    return tmp_path


def test_build_index_modules_and_endpoints(ws):
    idx = build_index(ws, ["python", "javascript"])
    mods = idx["modules"]
    assert "user" in mods and "order" in mods, f"模块切分: {list(mods)}"
    assert mods["user"]["symbols_n"] >= 2  # login + UserStore
    assert len(idx["endpoints"]) >= 2, "express get/post 两端点应入册"
    assert any(e["path"] == "/api/users" for e in idx["endpoints"])
    assert idx["built_at"]


def test_match_requirement_module_direct(ws):
    idx = build_index(ws, ["python", "javascript"])
    hit = match_requirement(idx, "测试 order 模块的边界")
    assert hit["kind"] == "module" and hit["module"] == "order"


def test_match_requirement_alias_to_user(ws):
    """别名路由：需求说'登录'→命中 user 模块（模块符号含 login）。"""
    idx = build_index(ws, ["python", "javascript"])
    hit = match_requirement(idx, "测试登录功能的非法输入")
    assert hit["kind"] == "module" and hit["module"] == "user"


def test_match_requirement_none_falls_back(ws):
    idx = build_index(ws, ["python", "javascript"])
    hit = match_requirement(idx, "测试完全不相关的板块 xyz")
    assert hit["kind"] == "none", "未命中必须回退全库（不错路由）"


def test_ensure_index_fingerprint_reuse(tmp_path, ws, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    idx1 = ensure_index(7, ws, ["python", "javascript"])
    assert (tmp_path / "index" / "project_7.json").exists()
    built1 = idx1["built_at"]
    idx2 = ensure_index(7, ws, ["python", "javascript"])
    assert idx2["built_at"] == built1, "指纹未变必须复用（一劳永逸语义）"


def test_state_declares_index_keys():
    """366 教训：未声明键被 LangGraph schema 过滤——AST 锁。"""
    src = Path(__file__).resolve().parents[1].joinpath(
        "test_agent", "graph", "state.py").read_text(encoding="utf-8")
    assert "index_scope_paths" in src and "index_endpoints" in src


def test_nodes_analyze_wires_index():
    """nodes analyze 必须接线索引路由（AST 探针，抄 state_wiring_locks 模式）。"""
    src = Path(__file__).resolve().parents[1].joinpath(
        "test_agent", "graph", "nodes.py").read_text(encoding="utf-8")
    assert "match_requirement" in src and "ensure_index" in src
    assert "index_scope_paths" in src
