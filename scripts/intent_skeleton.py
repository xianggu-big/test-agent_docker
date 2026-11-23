# -*- coding: utf-8 -*-
"""方案 1（2026-11-23 外部 AI 协同定稿）：盲意图骨架裁剪器。

"意图通道（Blind to Body）"：剥离函数体，只留签名+类型注解+docstring+异常
注释——LLM 站在用户契约视角列边界清单，不被 buggy 实现镜像（把错阈值当规格）。
配套"调用点挖掘"（Q4）：仓库内调用该符号的上游代码行=调用方假设证据。

输出：{"status": "ok", "skeletons": [...], "call_sites": [...]} 或 no_targets 降级。
只读。防镜像铁律：输出【绝不包含】任何函数体语句。
"""
import argparse
import ast
import json
import sys
from pathlib import Path


def _skeleton(node: ast.FunctionDef | ast.AsyncFunctionDef, src_lines) -> dict | None:
    doc = ast.get_docstring(node) or ""
    try:
        sig = f"def {node.name}({ast.unparse(node.args)})" + (
            f" -> {ast.unparse(node.returns)}" if node.returns else "")
    except Exception:   # noqa: BLE001
        sig = f"def {node.name}(...)"
    raises = sorted({ast.unparse(h.exc)[:60] if h.exc else "bare"
                     for h in ast.walk(node) if isinstance(h, ast.Raise)})
    if not doc and not raises:
        # 薄文档且无 raise 声明——仍保留签名（意图最少有类型形状）
        pass
    return {"signature": sig, "docstring": doc[:400],
            "raises": raises[:5], "line": node.lineno}


def extract(repo: Path, target_names: set[str] | None = None) -> dict:
    skeletons, call_sites = [], []
    n_scanned = 0
    for py in sorted(repo.rglob("*.py")):
        low = py.as_posix().lower()
        if any(s in low for s in ("/test", "/tests/", "/docs/", "/examples/")) or \
                py.name.startswith(("test_", "setup")):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = py.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if target_names is None or node.name in target_names:
                    sk = _skeleton(node, None)
                    sk["where"] = f"{rel}::{node.name}"
                    skeletons.append(sk)
                    n_scanned += 1
            elif isinstance(node, ast.Call):
                f = node.func
                name = getattr(f, "attr", None) or getattr(f, "id", None)
                if name and target_names and name in target_names:
                    call_sites.append({"callee": name, "where": rel,
                                       "line": node.lineno})
    if not skeletons:
        return {"status": "no_targets", "skeletons": [], "call_sites": []}
    return {"status": "ok",
            "skeletons": skeletons[:12],
            "call_sites": call_sites[:15]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--targets", help="逗号分隔的被测符号名；缺省=顶层全部（限 12 个）")
    args = ap.parse_args()
    repo = Path(args.repo_root)
    names = {t.strip() for t in args.targets.split(",")} if args.targets else None
    out = extract(repo, names)
    print(json.dumps(out, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
