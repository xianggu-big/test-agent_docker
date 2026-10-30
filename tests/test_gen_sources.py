# -*- coding: utf-8 -*-
"""被测源码收集（D1/D5）与静态闸包模块映射（D3）——2026-10-31 真实项目跑测短板修复。"""
import ast
import textwrap

import pytest

from test_agent.skills import gen_tests as g
from test_agent.skills.gen_tests import (_collect_sources, _collect_sources_detailed,
                                         _is_noise_source, _module_top_level,
                                         _split_top_level, _static_filter)
from pathlib import Path


def _mkws(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


# ---------- D5：打包/测试运行器文件不进被测源码 ----------

def test_noise_files_excluded(tmp_path):
    ws = _mkws(tmp_path, {
        "setup.py": "print('pkg')\n",
        "conftest.py": "X = 1\n",
        "tests.py": "def test_a():\n    pass\n",
        "biz.py": "def real():\n    return 1\n",
    })
    srcs = _collect_sources(ws)
    paths = [p for p, _ in srcs]
    assert paths == ["biz.py"]


def test_noise_predicate_unit():
    assert _is_noise_source(Path("setup.py"))
    assert _is_noise_source(Path("TESTS.py"))          # 大小写不敏感
    assert _is_noise_source(Path("test_foo.py"))
    assert _is_noise_source(Path("foo_test.py"))
    assert not _is_noise_source(Path("titlecase.py"))  # test_ 前缀误伤防线


# ---------- D1①：小文件整收、大文件切片、模块级常量保留 ----------

def test_small_file_whole(tmp_path):
    body = "def a():\n    return 1\n"
    ws = _mkws(tmp_path, {"m.py": body})
    srcs = _collect_sources(ws)
    assert srcs == [("m.py", body)]


def test_large_file_chunked_with_module_level(tmp_path):
    # > WHOLE_FILE_LIMIT：顶部常量 + 多个函数 → 切片且常量段保留
    big = "SMALL_WORDS = {'a', 'of'}\n\n" + "".join(
        f"def f{i}():\n    return {i}\n\n\n" for i in range(600))
    ws = _mkws(tmp_path, {"bigmod.py": big})
    assert len(big) > g.WHOLE_FILE_LIMIT
    chunks = _split_top_level(big)
    names = [n for n, _ in chunks]
    assert names[0] == "(module-level)"
    assert "SMALL_WORDS" in chunks[0][1]          # 词表不丢
    assert "f0" in names and "f399" in names
    for _, seg in chunks:                          # 每段可独立解析
        ast.parse(seg)
    srcs = _collect_sources(ws)
    keys = [p for p, _ in srcs]
    assert "bigmod.py::(module-level)" in keys
    assert "bigmod.py::f399" in keys
    assert all(not k.endswith("bigmod.py") for k in keys)  # 不再整文件收录


def test_budget_truncation_recorded_not_silent(tmp_path, monkeypatch):
    big1 = "".join(f"def g{i}():\n    return {i}\n\n\n" for i in range(300))
    ws = _mkws(tmp_path, {"a.py": big1, "b.py": big1})
    monkeypatch.setattr(g, "MAX_SOURCE_CHARS", 800)
    srcs, skipped = _collect_sources_detailed(ws)
    assert skipped, "预算截断必须留台账（旧版静默 continue 是 H1 假缺陷根因）"
    assert all("（预算截断）" in s or "超预算" in s for s in skipped)
    used = sum(len(t) for _, t in srcs)
    assert used <= g.MAX_SOURCE_CHARS


def test_syntax_error_file_skipped_not_crash(tmp_path):
    ws = _mkws(tmp_path, {"broken.py": "def f(:\n", "ok.py": "def ok():\n    return 1\n"})
    srcs, skipped = _collect_sources_detailed(ws)
    assert [("ok.py", "def ok():\n    return 1\n")] == srcs
    assert any("broken.py" in s for s in skipped)


def test_compat_shell_returns_list(tmp_path):
    """repair_strategy 复写路径共用 _collect_sources 的契约：返回 [(path, text)]。"""
    ws = _mkws(tmp_path, {"m.py": "def a():\n    return 1\n"})
    out = _collect_sources(ws)
    assert isinstance(out, list) and out and out[0][0] == "m.py"


# ---------- D3：包入口 __init__.py 以包目录名进符号表 ----------

def test_static_gate_package_module(tmp_path):
    ws = _mkws(tmp_path, {
        "titlecase/__init__.py": "def titlecase(text):\n    return text\n",
        "titlecase/tests.py": "raise RuntimeError('不该被扫描为业务源')\n",
    })
    table = _module_top_level(ws)
    assert table.get("titlecase") == {"titlecase"}          # 包名映射生效
    assert "__init__" not in table
    ok, rejected = _static_filter(
        [{"name": "t1", "code": "from titlecase import titlecase\n\n\ndef test_t1():\n    assert titlecase('a') == 'A'\n"}],
        ws)
    assert ok and not rejected
    ok2, rejected2 = _static_filter(
        [{"name": "t2", "code": "from titlecase import nonexistent_symbol\n\n\ndef test_t2():\n    pass\n"}],
        ws)
    assert not ok2 and any("nonexistent_symbol" in r for r in rejected2)  # 闸真正生效


def test_static_gate_flat_module_unchanged(tmp_path):
    """平铺模块行为不变（老口径回归）。"""
    ws = _mkws(tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"})
    table = _module_top_level(ws)
    assert table == {"calc": {"add"}}
    ok, _ = _static_filter(
        [{"name": "t", "code": "from calc import add\n\n\ndef test_t():\n    assert add(1, 2) == 3\n"}],
        ws)
    assert ok
