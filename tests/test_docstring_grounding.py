"""C 源·docstring grounding 单测（领域知识注入调研 §C）：提取/注入/开关三件事。"""
from pathlib import Path

from test_agent.skills.gen_tests import _extract_api_docs
from test_agent.skills.prompt_library import build_user_prompt

_SRC = '''"""优惠券模块：校验与计算。"""

def check_length(pw: str) -> bool:
    """长度校验。

    Args:
        pw: 密码字符串。
    Returns:
        长度在 [8, 64] 内返回 True。
    """
'''

_SRC_EMPTY = "def f(x):\n    return x\n"


def _ws(tmp_path: Path, src: str) -> Path:
    (tmp_path / "mod.py").write_text(src, encoding="utf-8")
    return tmp_path


def test_extract_includes_module_and_function_docs(tmp_path):
    out = _extract_api_docs(_ws(tmp_path, _SRC))
    assert out is not None
    assert "### mod（模块）" in out and "优惠券模块" in out
    assert "### mod.check_length" in out and "Args" in out


def test_extract_budget_prefers_semantic_docs(tmp_path):
    src = '"""\n' + "占位" * 400 + '\n"""\n\ndef good(p):\n    """Args:\n        x: 参数。\n    Returns:\n        y。\n    """\n'
    out = _extract_api_docs(_ws(tmp_path, src), max_chars=300)
    assert out is not None and "good" in out and "占位" not in out   # 优质段优先保留


def test_extract_none_when_no_docstrings(tmp_path):
    assert _extract_api_docs(_ws(tmp_path, _SRC_EMPTY)) is None


def test_prompt_injects_api_docs_section_with_discipline():
    src = [("m.py", "def f(x):\n    return x\n")]
    on = build_user_prompt(src, "需求", api_docs="### m.f\nArgs: x")
    assert "API 行为文档" in on and "docstring文档" in on and "### m.f" in on
    off = build_user_prompt(src, "需求")
    assert "API 行为文档" not in off


def test_prompt_without_api_docs_unchanged():
    src = [("m.py", "def f(x):\n    return x\n")]
    a = build_user_prompt(src, "需求", api_docs=None)
    b = build_user_prompt(src, "需求")
    assert a == b                                                   # 开关关=逐字节不变
