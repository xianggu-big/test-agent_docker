# -*- coding: utf-8 -*-
"""端点缺口回灌测试锁（2026-11-19 复盘方案 A 收尾：缺口注入生成提示词）。

方案 A 前半（对账+升级 stop）由 test_api_blackbox_endpoint_audit.py 锁；
本文件锁后半：缺口清单真的到达生成提示词（evaluate→state→generate→prompt 全链）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.skills.prompt_library import build_user_prompt

_SRC = [("mod/a.py", "def add(a, b):\n    return a + b\n")]


def test_prompt_contains_gap_section():
    out = build_user_prompt(_SRC, "测试 books 接口",
                            endpoint_gaps=["DELETE /books/{p}", "POST /books"])
    assert "## 端点缺口补测" in out
    assert "- DELETE /books/{p}" in out and "- POST /books" in out
    assert "禁止虚构清单外端点" in out


def test_prompt_without_gaps_byte_clean():
    """缺省=不注入该段（既有提示词逐字节不变纪律）。"""
    out = build_user_prompt(_SRC, "测试 books 接口")
    assert "端点缺口补测" not in out
    out2 = build_user_prompt(_SRC, "测试 books 接口", endpoint_gaps=None)
    assert out == out2


def test_gap_section_capped_at_8():
    gaps = [f"GET /only{i}" for i in range(12)]
    out = build_user_prompt(_SRC, "测试", endpoint_gaps=gaps)
    assert "- GET /only7" in out and "- GET /only8" not in out   # 封顶 8 条防爆


def test_ast_locks_full_chain():
    """AST 探针：evaluate 写缺口→返回体带键→generate 透传→state 声明→gen 消费。"""
    root = Path(__file__).resolve().parents[1]
    nodes = (root / "test_agent" / "graph" / "nodes.py").read_text(encoding="utf-8")
    assert 'state["index_endpoint_gaps"]' in nodes, "evaluate 必须写缺口键"
    assert '"index_endpoint_gaps"' in nodes, "evaluate 返回体必须带键（366 教训）"
    assert '"endpoint_gaps": state.get("index_endpoint_gaps")' in nodes, \
        "generate 必须透传给 gen_tests"
    state_src = (root / "test_agent" / "graph" / "state.py").read_text(encoding="utf-8")
    assert "index_endpoint_gaps" in state_src, "TaskState 必须声明（schema 过滤=丢弃）"
    gen_src = (root / "test_agent" / "skills" / "gen_tests.py").read_text(encoding="utf-8")
    assert 'args.get("endpoint_gaps")' in gen_src, "gen_tests 必须消费缺口"
