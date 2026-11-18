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


def test_match_endpoints_not_swallowed_by_whole_repo_module(tmp_path):
    """单模块仓（文件数=全库）不参与模块路由——"测所有 REST 接口" 必须走接口对账
    （E2E-1 预演实锤：go-rest-api-master 单模块把 rest 别名吞进模块路由）。"""
    (tmp_path / "go-rest-api-master").mkdir()
    (tmp_path / "go-rest-api-master" / "main.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8")
    idx = build_index(tmp_path, ["go"])
    idx["endpoints"] = [{"method": "GET", "path": "/albums"}]
    hit = match_requirement(idx, "测所有 REST 接口")
    assert hit["kind"] == "endpoints" and hit["count"] == 1, f"单模块不得吞接口路由: {hit}"


def test_match_endpoints_intent_beats_transport_word_in_blob(tmp_path):
    """api 组=接口意图词，不参与模块别名匹配（多模块仓 blob 混入 rest 词也不吞）。"""
    (tmp_path / "internal" / "handlers").mkdir(parents=True)
    (tmp_path / "internal" / "handlers" / "albums.go").write_text(
        "package handlers\n\nfunc GetAlbums() {}\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "store.go").write_text(
        "package store\n\nfunc Save() {}\n", encoding="utf-8")
    idx = build_index(tmp_path, ["go"])
    idx["endpoints"] = [{"method": "GET", "path": "/albums"},
                        {"method": "POST", "path": "/albums"}]
    hit = match_requirement(idx, "测所有 REST 接口")
    assert hit["kind"] == "endpoints" and hit["count"] == 2, f"意图词不得被 blob 吞: {hit}"


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


# ---------------- L3 失败语料自学习（2026-11-18 设计方案 §3.3） ----------------

_GOLDEN_INDEX_MODULES = {
    "user":      ["login", "register"],
    "order":     ["create_order"],
    "pay":       ["charge"],
    "inventory": ["deduct"],
    "logger":    ["log_info"],
    "file":      ["save_upload"],
    "search":    ["dedup"],
    "export":    ["to_csv"],
}


def _golden_index() -> dict:
    """金样合成索引：模块名=常见业务域+各 1 个符号+一份端点清单（单索引过全部金样）。"""
    mods = {m: {"files": [f"{m}/svc.py"], "symbols": syms, "loc": 20,
                "symbols_n": len(syms), "files_n": 1}
            for m, syms in _GOLDEN_INDEX_MODULES.items()}
    mods["misc"] = {"files": ["misc/util.py"], "symbols": ["helper"], "loc": 10,
                    "symbols_n": 1, "files_n": 1}
    return {"modules": mods,
            "endpoints": [{"method": "GET", "path": "/api/items", "source": "app.js"}]}


@pytest.fixture(scope="module")
def golden_cases():
    p = Path(__file__).resolve().parent / "golden_alias_cases.json"
    return json.loads(p.read_text(encoding="utf-8"))


def test_l3_golden_alias_hitrate(golden_cases):
    """L3-4 金样回归：10 条人工标定期望，路由命中率不得回退（改表后必须全跑）。"""
    idx = _golden_index()
    hits, misses = 0, []
    for case in golden_cases:
        hit = match_requirement(idx, case["req"])
        kind = hit.get("kind")
        if "expect_kind" in case:
            if kind == case["expect_kind"]:
                hits += 1
            else:
                misses.append(f"{case['req']} → kind={kind}（期望 {case['expect_kind']}）")
            continue
        module = (hit.get("module") or "") if kind == "module" else ""
        if any(sub in module for sub in case["expect_module_contains"]):
            hits += 1
        else:
            misses.append(f"{case['req']} → module={module or kind}"
                          f"（期望含 {case['expect_module_contains']}）")
    assert hits == len(golden_cases), \
        f"金样命中率 {hits}/{len(golden_cases)} 回退：{misses}"


def test_l3_alias_snapshot_lock():
    """L3-3 别名快照锁：改 _ALIAS_GROUPS 不重录快照=测试红（防无审漂移）。"""
    snap_file = Path(__file__).resolve().parent / "alias_groups_snapshot.json"
    snap = snap_file.read_text(encoding="utf-8").strip()
    from test_agent.skills.project_index import _ALIAS_GROUPS
    now = json.dumps(_ALIAS_GROUPS, ensure_ascii=False, sort_keys=True, indent=2)
    assert now == snap, (
        "_ALIAS_GROUPS 与快照不一致——别名表改动必须走人工审核流程：\n"
        "1) 依据 data/reports/e10/alias_suggestions.json 候选逐条拍板；\n"
        "2) 改 project_index._ALIAS_GROUPS（一行一词，评审可见）；\n"
        "3) 重跑 python -c \"json.dump(...)\" 重录 tests/alias_groups_snapshot.json；\n"
        "4) 跑 tests/test_l3_golden_alias_hitrate 金样回归确认命中率不降。")


def test_l3_suggest_aliases_binds_real_modules():
    """L3-2 候选只含真实簇：模块绑定候选必须 ∈ 簇项目模块清单（合成簇驱动脚本函数）。"""
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "suggest_aliases", root / "scripts" / "suggest_aliases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mods = {"booking": {"files": ["booking/cls.py"], "symbols": ["book_room"], "loc": 30},
            "_root": {"files": ["app.py"], "symbols": ["main"], "loc": 5}}
    sym2mod = {"book_room": "booking"}
    root_syms = ["main"]
    cands = mod._candidates(
        ["AssertionError: assert book_room('x') == 1", "main() crashed"],
        mods, sym2mod, root_syms)
    by_word = {c["suggested_alias"]: c for c in cands}
    assert by_word["book_room"]["suggested_module"] == "booking"   # 真模块绑定
    assert by_word["main"]["suggested_module"] is None             # 根级符号=无归属（如实）
    assert by_word["book_room"]["evidence_n"] == 1
    # L3-2 护栏语义：绑定候选的模块必须 ∈ 模块清单（脚本内 assert 的单测对偶）
    assert all(c["suggested_module"] in mods
               for c in cands if c["suggested_module"] is not None)
