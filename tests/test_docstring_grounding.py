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


# ---------- Go godoc 提取（任务卡-F2 阶段 1 #5：C 源 godoc 去 Python 锁） ----------

def _ws_go(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_go_godoc_提取签名与注释(tmp_path):
    out = _extract_api_docs(_ws_go(tmp_path, {
        "shop/order.go": ('// Package shop 订单分级服务。\n'
                          'package shop\n\n'
                          '// GetTier 按积分分级：>=90 "A"，>=60 "B"，其余 "C"。\n'
                          '// Returns: 分级字符串。\n'
                          'func GetTier(score int) string {\n'
                          '\tif score > 90 {\n\t\treturn "A"\n\t}\n\treturn "C"\n}\n\n'
                          '// Add 加法（对照函数）。\n'
                          'func Add(a, b int) int {\n\treturn a + b\n}\n'),
        "shop/order_test.go": '// 测试文件不应被提取\nfunc TestX(t *testing.T) {}\n',
    }), lang="go")
    assert out is not None
    assert "### shop.GetTier" in out and '>=90 "A"' in out       # 注释体进提示词
    assert "func GetTier(score int) string" in out               # 函数签名
    assert "### shop.Add" in out
    assert "TestX" not in out and "order_test" not in out        # 测试文件不提取


def test_go_godoc_无注释返回None(tmp_path):
    out = _extract_api_docs(_ws_go(tmp_path, {
        "bare/main.go": "package main\n\nfunc main() {}\n"}), lang="go")
    assert out is None


def test_go_godoc_方法接收者签名(tmp_path):
    out = _extract_api_docs(_ws_go(tmp_path, {
        "cart/cart.go": ('package cart\n\n'
                         '// Total 计算合计。\n'
                         'func (c *Cart) Total() int {\n\treturn 0\n}\n')}), lang="go")
    assert out is not None and "func (c *Cart) Total()" in out


def test_auto语言探测_py优先_go兜底(tmp_path):
    out = _extract_api_docs(_ws_go(tmp_path, {
        "shop/order.go": '// GetTier 分级。\nfunc GetTier(score int) string {\n\treturn "A"\n}\n'}))
    assert out is not None and "GetTier" in out                   # 无 lang 参数=自动探测
