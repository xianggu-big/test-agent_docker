# -*- coding: utf-8 -*-
"""方案 D（2026-11-23 外部 AI 协同定稿）：仓库原生测试线束卡抽取器。

设计（第二轮对账 Q1 收敛）——"结构-语义物理剥离 + 负空间排斥"：
- 只给【机械结构】不给【测试内容】：import 形态/夹具与基类签名/断言语法习惯
  /异常类清单——治 A 类（断言瞎猜）与 B 类（no_tests 脚手架）
- 刻意不给常规 happy-path 用例函数体（防止把仓库测试的盲区也学走——
  它们恰是没发现 bug 的人写的）；异常偏置片段只给【函数名清单】不给体
- 负空间数据：已通过测试名清单（供提示词组装"排除表+漏缺陷声明"）

输出（stdout 单行 JSON，先例 jacoco_multimodule CLI 契约）：
  {"status": "ok", "card": {...}, "names_excluded": [...], "n_test_files": N}
错误码：E_NO_TESTS（无测试文件，status=no_tests 诚实降级）/ E_BAD_SRC。
只读：永不写被测项目文件。

用法：
  python -I scripts/harness_card.py <repo_root> [--json-out PATH]
"""
import argparse
import ast
import json
import sys
from pathlib import Path

# 异常/边界偏置词（检索名含这些的测试只报名不报体——happy-path 全部不报）
BIAS_WORDS = ("raise", "error", "invalid", "fail", "edge", "boundary",
              "except", "empty", "null", "none", "missing", "malform", "timeout")

# 断言语法习惯统计的识别器（粗粒度——签名级信息，非内容）
ASSERT_FORMS = {
    "pytest.raises": "异常断言 pytest.raises(...)",
    "assertAlmostEqual": "近似断言 assertAlmostEqual(...)",
    "assertIn": "成员断言 assertIn(...)",
    "assertIsNone": "None 断言 assertIsNone(...)",
    "pytest.approx": "容差断言 pytest.approx(...)",
    "parametrize": "参数化 @pytest.mark.parametrize",
    "monkeypatch": "猴子补丁 monkeypatch fixture",
    "tmp_path": "临时目录 tmp_path fixture",
}


def extract_card(repo: Path) -> dict:
    test_files = sorted(
        p for p in repo.rglob("*.py")
        if p.name.startswith("test_") or p.name.endswith("_test.py"))
    if not test_files:
        return {"status": "no_tests", "card": {}, "names_excluded": [],
                "n_test_files": 0}

    imports = set()          # (module, names) 摘要
    fixtures = []            # 夹具/基类签名
    assert_usage = {}        # 形态 → 次数
    names_excluded = []      # 全部测试函数名（负空间排除表）
    biased_names = []        # 异常/边界偏置的测试名（只报名）
    test_classes = []        # 测试基类/子类签名

    for tf in test_files[:40]:          # 封顶防大仓
        try:
            tree = ast.parse(tf.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = tf.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.add(("import", a.name))
            elif isinstance(node, ast.ImportFrom):
                names = ", ".join(a.name for a in node.names)
                imports.add((f"from {node.module}" if node.module else "from .",
                             names))
            elif isinstance(node, ast.FunctionDef):
                deco_src = ""
                for d in node.decorator_list:
                    try:
                        deco_src += ast.unparse(d) + ";"
                    except Exception:   # noqa: BLE001
                        pass
                src_head = ""
                try:
                    src_head = ast.unparse(node.args)
                except Exception:       # noqa: BLE001
                    pass
                if any(w in node.name.lower() for w in BIAS_WORDS):
                    biased_names.append(f"{rel}::{node.name}")
                if node.name.startswith("test"):
                    names_excluded.append(f"{rel}::{node.name}")
                elif "fixture" in deco_src:
                    fixtures.append(f"@{deco_src.rstrip(';')} def {node.name}({src_head})")
                args = {a.arg for a in node.args.args}
                if {"monkeypatch", "tmp_path", "capsys"} & args:
                    used = sorted({"monkeypatch", "tmp_path", "capsys"} & args)
                    fixtures.append(f"def {node.name}({src_head})  # pytest 内建夹具: {used}")
            elif isinstance(node, ast.ClassDef):
                bases = []
                for b in node.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:   # noqa: BLE001
                        pass
                if bases and ("Test" in node.name or any("test" in x.lower() for x in bases)):
                    test_classes.append(f"class {node.name}({', '.join(bases)})")
        text = tf.read_text(encoding="utf-8", errors="replace")
        for form, desc in ASSERT_FORMS.items():
            if form in text:
                assert_usage[desc] = assert_usage.get(desc, 0) + text.count(form)

    card = {
        "imports_sample": sorted(f"{k} {v}" for k, v in list(imports)[:25]),
        "fixture_signatures": fixtures[:8],
        "test_class_signatures": test_classes[:6],
        "assert_style": sorted(assert_usage.items(), key=lambda kv: -kv[1])[:6],
        "biased_test_names": biased_names[:10],
        "n_test_files": len(test_files),
    }
    return {"status": "ok", "card": card,
            "names_excluded": names_excluded[:120], "n_test_files": len(test_files)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--json-out")
    args = ap.parse_args()
    repo = Path(args.repo_root)
    if not repo.is_dir():
        print(json.dumps({"status": "error", "code": "E_BAD_SRC"}))
        return 2
    out = extract_card(repo)
    line = json.dumps(out, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print(line[:600] + ("…" if len(line) > 600 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
