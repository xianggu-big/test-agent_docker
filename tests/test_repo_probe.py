# -*- coding: utf-8 -*-
"""方案 D+1+4 注入段（2026-11-23）：两态测试——默认关=逐字节不变，开=三段全在。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_agent.skills.prompt_library import (  # noqa: E402
    _repo_probe_section, build_user_prompt)

SRC = [("calc.py", "def add(a, b):\n    return a + b\n")]
HC = {
    "imports_sample": ["from x import y"],
    "fixture_signatures": ["def fx(tmp_path)"],
    "assert_style": [("异常断言 pytest.raises(...)", 3)],
    "biased_test_names": ["test_raise_on_bad"],
    "names_excluded": ["tests/test_a.py::test_happy"],
    "n_test_files": 3,
}
SK = [{"signature": "def format_sizeof(num, suffix='', divisor=1000)",
       "docstring": "Formats a number with SI prefixes.",
       "raises": [], "where": "tqdm/std.py::format_sizeof"}]
CS = [{"callee": "format_sizeof", "where": "tqdm/std.py", "line": 300}]


def test_section_empty_when_all_none():
    assert _repo_probe_section(None, None, None) == ""
    assert _repo_probe_section({}, [], []) == ""


def test_section_contains_all_five_blocks():
    s = _repo_probe_section(HC, SK, CS)
    assert "仓库测试线束卡" in s
    assert "负空间排除表" in s and "漏掉了潜伏缺陷" in s
    assert "意图骨架" in s and "实现体刻意不可见" in s
    assert "调用方假设" in s
    assert "蜕变/不变量优先" in s and "禁猜魔数" in s
    assert "def format_sizeof" in s


def test_prompt_default_off_byte_identical():
    a = build_user_prompt(SRC, "测 add", ctx_budget=None)
    b = build_user_prompt(SRC, "测 add", ctx_budget=None,
                          harness_card=None, intent_skeletons=None, call_sites=None)
    assert a == b, "默认参数必须逐字节不变"


def test_prompt_with_probe_longer_and_marked():
    base = build_user_prompt(SRC, "测 add", ctx_budget=None)
    withp = build_user_prompt(SRC, "测 add", ctx_budget=None,
                              harness_card=HC, intent_skeletons=SK, call_sites=CS)
    assert len(withp) > len(base)
    assert "仓库测试线束卡" in withp and "负空间排除表" in withp
