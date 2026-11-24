# -*- coding: utf-8 -*-
"""模块级画像与策略建议 A 版（任务卡-E）单元测试。

口径锁：切分 4 语言规则/Top15 上限与 excluded 如实标注/纯数据跳过/规则表七条
逐条/权重×2/LLM 兜底理由落库且 fail-open 不编造/落库幂等/旗标关=analyze 只写
'.' 整仓行（旧行为逐字节不变）。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config
from test_agent.skills.module_profile import (analyze_modules, persist,
                                              profile_module, split_modules,
                                              suggest_routes)


def _mk(ws: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


# ---------- 切分：4 语言 ----------

def test_切分_python_pkg目录与顶层散文件(tmp_path):
    _mk(tmp_path, {
        "pkg_a/__init__.py": "", "pkg_a/core.py": "x = 1\n",
        "lone.py": "y = 2\n", "pkg_a/test_core.py": "",   # 测试文件不进切分
    })
    kept, excluded = split_modules(tmp_path, "python")
    paths = {m["path"] for m in kept}
    assert "pkg_a" in paths and "lone.py" in paths      # pkg 目录+顶层散文件
    assert all(not any("test_" in f for f in m["files"]) for m in kept)


def test_切分_js_src子目录_go_package_html逐页(tmp_path):
    _mk(tmp_path, {"src/handlers/a.js": "export const x=1\n",
                   "src/utils/b.js": "export const y=2\n",
                   "src/index.js": "export default 1\n"})
    kept, _ = split_modules(tmp_path, "javascript")
    assert {m["path"] for m in kept} >= {"src/handlers", "src/utils"}   # src 下一级目录

    ws2 = tmp_path / "go_ws"
    _mk(ws2, {"auth/login.go": "package auth\n", "auth/token.go": "package auth\n",
              "main.go": "package main\n"})
    kept2, _ = split_modules(ws2, "go")
    assert {m["path"] for m in kept2} >= {"auth", "."}          # package 目录+根包

    ws3 = tmp_path / "html_ws"
    _mk(ws3, {"index.html": "<html></html>", "students.html": "<html></html>"})
    kept3, _ = split_modules(ws3, "unknown")
    assert {m["path"] for m in kept3} == {"index.html", "students.html"}  # 每页面一模块


def test_切分_Top15上限_excluded如实(tmp_path):
    files = {}
    for i in range(20):
        files[f"m{i:02d}/__init__.py"] = ""
        files[f"m{i:02d}/code.py"] = f"def f{i}():\n    return {i}\n"
    ws = tmp_path / "big"; _mk(ws, files)
    kept, excluded = split_modules(ws, "python")
    assert len(kept) == 15
    assert len(excluded) == 5 and all("path" in e and "files" in e for e in excluded)


# ---------- 画像与规则表 ----------

def _profile_of(ws, lang="python"):
    kept, _ = split_modules(ws, lang)
    assert kept, "切分结果为空"
    return profile_module(ws, kept[0], kept), kept


def test_画像_纯函数带docstring_规则单元(tmp_path):
    _mk(tmp_path, {"utils/__init__.py": "",
                   "utils/text.py": '"""文本工具。"""\n\ndef clip(v, lo, hi):\n    """裁剪。"""\n    return max(lo, min(v, hi))\n'})
    kept, _ = split_modules(tmp_path, "python")
    mod = next(m for m in kept if m["path"] == "utils")
    p = profile_module(tmp_path, mod, kept)
    assert p["kind"] == "pure_func" and p["has_docstring"] and not p["pure_data"]
    sugg, reason = suggest_routes(p)
    assert any(s["route"] == "l1_unit_pytest" for s in sugg)
    assert "纯函数+有规格" in reason


def test_画像_纯函数无规格_规则属性加差分(tmp_path):
    _mk(tmp_path, {"bare.py": "def shrink(v):\n    return v - 1\n"})
    p, _ = _profile_of(tmp_path)
    sugg, reason = suggest_routes(p)
    routes = {s["route"] for s in sugg}
    assert {"property_based", "cli_reference_diff"} <= routes
    assert "无规格" in reason


def test_画像_api_handler_规则契约场景(tmp_path):
    _mk(tmp_path, {"api/__init__.py": "",
                   "api/routes.py": "from fastapi import APIRouter\nr = APIRouter()\n\n@r.get('/x')\ndef x():\n    return 1\n"})
    kept, _ = split_modules(tmp_path, "python")
    mod = next(m for m in kept if m["path"] == "api")
    p = profile_module(tmp_path, mod, kept)
    assert p["kind"] == "api_handler" and p["web_entry"]
    sugg, reason = suggest_routes(p)
    assert any(s["route"] == "l2_contract_http" for s in sugg) and "API handler" in reason


def test_画像_db_access_规则db_assert(tmp_path):
    _mk(tmp_path, {"db/__init__.py": "",
                   "db/store.py": "import sqlite3\n\ndef save(r):\n    conn = sqlite3.connect('x.db')\n    conn.execute('INSERT INTO t VALUES (?)', (r,))\n"})
    kept, _ = split_modules(tmp_path, "python")
    mod = next(m for m in kept if m["path"] == "db")
    p = profile_module(tmp_path, mod, kept)
    assert p["db_calls"]
    sugg, reason = suggest_routes(p)
    assert any(s["route"] == "db_assert" for s in sugg) and "DB" in reason


def test_画像_纯数据_规则跳过(tmp_path):
    _mk(tmp_path, {"config.py": "DEBUG = 1\nTIMEOUT = 30\n"})
    p, _ = _profile_of(tmp_path)
    assert p["pure_data"] and p["kind"] == "config"
    sugg, reason = suggest_routes(p)
    assert sugg == [{"route": "skip", "weight": 0.0,
                     "reason": "纯数据/配置，无可测行为"}]
    assert "纯数据跳过" in reason


def test_画像_页面_规则UI幂等(tmp_path):
    ws = tmp_path / "html_ws"
    _mk(ws, {"index.html": '<html><body><button id="b" onclick="f()">go</button></body></html>'})
    kept, _ = split_modules(ws, "unknown")
    p = profile_module(ws, kept[0], kept)
    sugg, reason = suggest_routes(p)
    assert any(s["route"] == "ui_interaction_idempotency" for s in sugg)
    assert "UI 交互" in reason


def test_权重翻倍_复杂度高且扇入高(tmp_path):
    _mk(tmp_path, {
        "hot/__init__.py": "", "hot/core.py":
            "def f(a):\n" + "    if a:\n        a -= 1\n" * 6 + "    return a\n",
        "user1.py": "import hot\n", "user2.py": "import hot\n", "user3.py": "import hot\n",
        "hot/__init___2.py": "",
    })
    kept, _ = split_modules(tmp_path, "python")
    mod = next(m for m in kept if m["path"] == "hot")
    p = profile_module(tmp_path, mod, kept)
    assert p["fan_in"] >= 3
    sugg, reason = suggest_routes(p)
    if p["complexity"] >= 10:                        # radon 可用时才锁翻倍
        assert "权重×2" in reason
        assert all(s["weight"] >= 2.0 for s in sugg)


def test_画像_注释里的路由字样不算API入口(tmp_path):
    """实测抓到的误判：全文正则把注释里的 @app.get 当成路由装饰器，无函数模块被判
    api_handler 并建议契约测试。Python 必须按 AST 看装饰器是否真挂在函数上。"""
    _mk(tmp_path, {"pkg/__init__.py": "",
                   "pkg/notes.py": "# 示例写法：@app.get('/x')\nX = 1\n"})
    kept, _ = split_modules(tmp_path, "python")
    mod = next(m for m in kept if m["path"] == "pkg")
    p = profile_module(tmp_path, mod, kept)
    assert p["web_entry"] is False and p["kind"] == "config"
    sugg, _ = suggest_routes(p)
    assert [s["route"] for s in sugg] == ["skip"]


def test_旗标默认关_零行为变化的前提():
    """analyze 接线整体在 if config.MODULE_PLAN_ENABLED 内（代码审计）；
    本测锁默认值=关（旗标关=只写 '.' 整仓行的旧行为）+ env 覆盖语义。"""
    assert config.MODULE_PLAN_ENABLED is False                      # 代码默认关
    assert config._flag("MTA_MODULE_PLAN", False) is False          # env 未设=默认
    try:
        import os
        os.environ["MTA_MODULE_PLAN"] = "1"
        assert config._flag("MTA_MODULE_PLAN", False) is True       # env 显式开
    finally:
        os.environ.pop("MTA_MODULE_PLAN", None)


def test_规则未命中_空建议走兜底(tmp_path):
    p = {"kind": "mixed", "pure_data": False, "web_entry": False, "db_calls": False,
         "has_docstring": False, "has_functions": True, "complexity": 1, "fan_in": 0}
    sugg, reason = suggest_routes(p)
    assert sugg == [] and reason == ""


# ---------- LLM 兜底 ----------

def test_兜底_llm可用_理由落库形态(tmp_path):
    from test_agent.skills import module_profile as mp
    mods = [{"path": "x", "files": ["x.py"]}]
    profiles = [{"loc": 10, "_has_suggestion": False}]
    out = mp.classify_fallback(tmp_path, mods, profiles,
                               llm_fn=lambda _p: json.dumps(
                                   {"0": {"kind_label": "业务规则",
                                          "reason": "承载业务规则"}}))
    assert out[0]["kind_label"] == "业务规则"
    assert "LLM 兜底分类" in out[0]["reason"]


def test_兜底_llm失败_failopen不编造(tmp_path):
    from test_agent.skills import module_profile as mp
    mods = [{"path": "x", "files": ["x.py"]}]
    profiles = [{"loc": 10, "_has_suggestion": False}]
    out = mp.classify_fallback(tmp_path, mods, profiles,
                               llm_fn=lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert out[0]["kind_label"] == "unclassified" and "LLM 兜底失败" in out[0]["reason"]
    out2 = mp.classify_fallback(tmp_path, mods, profiles, llm_fn=None)
    assert "不可用" in out2[0]["reason"]


def test_analyze_规则未命中_接线走兜底(tmp_path, monkeypatch):
    from test_agent.skills import module_profile as mp
    _mk(tmp_path, {"weird.py": "X = [1]\nY = [2]\n"})
    monkeypatch.setattr(mp, "suggest_routes", lambda p: ([], ""))   # 强制全未命中
    rep = mp.analyze_modules(tmp_path, "python",
                             llm_fn=lambda _p: json.dumps(
                                 {"0": {"kind_label": "胶水", "reason": "装配层"}}))
    m = rep["modules"][0]
    assert m["kind_label"] == "胶水" and "LLM 兜底分类" in m["reason"]


# ---------- 落库幂等 ----------

def test_落库_幂等重写(tmp_path):
    from test_agent.store.db import init_db
    db = tmp_path / "t.db"
    assert init_db(db) == 39                       # 39 表契约（40-42 包 2 + 43-44 T2）
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')")
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (1, 'r', 'n')")
    conn.commit()
    pid = conn.execute("SELECT id FROM projects").fetchone()[0]
    _mk(tmp_path, {"utils/__init__.py": "", "utils/t.py": '"""d."""\ndef f():\n    return 1\n'})
    rep = analyze_modules(tmp_path, "python", llm_fn=None)
    n1 = persist(conn, pid, rep)
    n2 = persist(conn, pid, rep)                   # 幂等：重写不叠加
    assert n1 == n2 == len(rep["modules"])
    rows = conn.execute("SELECT COUNT(*) FROM module_profiles WHERE project_id=?",
                        (pid,)).fetchone()[0]
    assert rows == len(rep["modules"])
    mrows = conn.execute("SELECT COUNT(*) FROM modules WHERE project_id=? AND path != '.'",
                         (pid,)).fetchone()[0]
    assert mrows == len(rep["modules"])            # modules 追加逐模块行
    conn.close()
