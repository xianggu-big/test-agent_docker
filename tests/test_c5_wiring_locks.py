# -*- coding: utf-8 -*-
"""C4/C5 多语言接线两颗运行时炸弹的 AST 锁（2026-11-13）。

背景：fc6406d/593aab3 两轮接线引入的雷都在「非 Python 语言的节点成功分支」里，
现有单测只覆盖模块级辅助函数（假 runner），节点闭包体没被执行到——全量回归抓不住。
本文件用 AST 对源码做静态锁，不依赖执行路径：

1. l2.provision 的 is_go 赋值被重构删掉但引用残留（UnboundLocalError）——
   锁：函数内 is_go 首次出现必须是赋值（Store），不得 Load 在前。
2. msg(state["session_id"], "assistant", ...) 三参调用（msg 只收两参，成功分支一进
   即 TypeError）——锁：l2.py/l3.py 全文件不得存在字面量第二参 "assistant" 的 msg 调用。
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GRAPH = Path(__file__).resolve().parent.parent / "test_agent" / "graph"


def _tree(name: str) -> ast.AST:
    return ast.parse((GRAPH / name).read_text(encoding="utf-8"))


def _find_fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def test_l2_provision_is_go先赋值后使用():
    fn = _find_fn(_tree("l2.py"), "provision")
    first_load_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "is_go":
            if isinstance(node.ctx, ast.Store):
                if first_load_line is not None and node.lineno > first_load_line:
                    continue
                return                                  # 首次出现即赋值 ✓
            first_load_line = node.lineno               # Load 在任何 Store 之前 → 炸弹
    if first_load_line is not None:
        raise AssertionError(f"provision 中 is_go 在赋值前被引用（行 {first_load_line}）"
                             "——fc6406d 的 UnboundLocalError 回归")


def _assert_no_three_arg_msg(source_name: str) -> None:
    tree = _tree(source_name)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "msg"):
            continue
        if len(node.args) >= 3:
            arg2 = node.args[1]
            shown = getattr(arg2, "value", None) or ast.dump(arg2)
            raise AssertionError(f"{source_name}:{node.lineno} msg 三参调用（msg 只收两参）"
                                 f"——第二参 {shown!r}，成功分支一进即 TypeError")


def test_l2_l3_无三参msg调用():
    _assert_no_three_arg_msg("l2.py")
    _assert_no_three_arg_msg("l3.py")
