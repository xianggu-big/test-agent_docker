# -*- coding: utf-8 -*-
"""G 人工对比协议·评分脚本（任务卡-G 材料件 3）单元测试。

口径锁：逐缺陷反事实差分（buggy 败 + 只修 Di 过 = 检出 Di）；5 修复模式必须
恰命中一处（否则如实报错不硬猜）；decoy 不进分母；净化版源码零答案键泄漏
且与原版 AST 等价（只许注释/文档串差异）。单测用假 runner（读文件即判），
不跑真 pytest——评分管线全真跑走验收（人工样例冒充）不做进单测。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.run_g_scoring import (apply_fix, collect_suite_files, load_ground_truth,
                                   load_project_sources, score_suite)
from test_agent import config

CORPUS = ROOT / "data" / "corpus" / "corpus-003"
PROJECT = CORPUS / "project"

# 答案键泄漏禁词（净化版与需求文本都不得出现）
LEAK_TOKENS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "ground_truth",
               "缺陷", "答案键", "种植")


# ---------- 修复模式 ----------

def test_五修复模式_各恰命中一处():
    srcs = load_project_sources(PROJECT)
    for did in ("D1", "D2", "D3", "D4", "D5"):
        fixed = apply_fix(srcs, did)          # 命中数≠1 会抛 ValueError
        assert fixed != srcs                  # 确实改了


def test_修复模式_未知缺陷如实报错():
    srcs = load_project_sources(PROJECT)
    with pytest.raises(ValueError):
        apply_fix(srcs, "D6")                 # decoy 不给修法


def test_decoy不进分母():
    gt = load_ground_truth(CORPUS)
    assert [d["id"] for d in gt["planted_defects"] if not d.get("decoy")] == \
        ["D1", "D2", "D3", "D4", "D5"]


# ---------- 净化版材料 ----------

def test_净化版零答案键泄漏():
    clean = ROOT / "docs" / "G材料" / "project_clean"
    assert clean.exists(), "净化版目录缺失"
    for py in clean.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for tok in LEAK_TOKENS:
            assert tok not in text, f"{py.name} 泄漏 {tok}"


def test_净化版与原版_AST等价():
    import ast
    clean = ROOT / "docs" / "G材料" / "project_clean"

    def ast_no_doc(src: str) -> str:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body[0].value.value = ""
        return ast.dump(tree)

    for name in ("app.py", "booking.py", "store.py"):
        a = ast_no_doc((PROJECT / name).read_text(encoding="utf-8"))
        b = ast_no_doc((clean / name).read_text(encoding="utf-8"))
        assert a == b, f"{name} 净化版改动了代码结构（只许注释/文档串差异）"


# ---------- 套件文件收集 ----------

def test_收集规则_只收测试形态文件(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "b_test.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("note\n", encoding="utf-8")
    got = collect_suite_files(tmp_path)
    assert [p.name for p in got] == ["b_test.py", "test_a.py"]   # helper/README 不收


# ---------- 评分管线（假 runner，读文件模拟反事实） ----------

class FakeRunner:
    """假 runner：D1 未修时 test_d1 败；D2 未修时 test_d2 败；其余全过。

    通过读工作区 booking.py 判断该缺陷是否已修复——与真 runner 的
    「跑套件看失败集」在反事实逻辑上同构，足以锁管线判定口径。
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, ws: Path):
        self.calls += 1
        src = (ws / "booking.py").read_text(encoding="utf-8")
        failed = []
        if "hours >= MAX_HOURS" in src:                 # D1 未修
            failed.append("test_d1.py::test_d1")
        if "* 0.85)" in src:                            # D2 未修
            failed.append("test_d2.py::test_d2")
        return ("1 failed, 1 passed" if failed else "2 passed", failed, 0.01)


def _mk_suite(tmp_path: Path) -> Path:
    s = tmp_path / "suite"
    s.mkdir()
    (s / "test_d1.py").write_text("def test_d1():\n    assert True\n", encoding="utf-8")
    (s / "test_d2.py").write_text("def test_d2():\n    assert True\n", encoding="utf-8")
    return s


def test_评分_两缺陷检出(tmp_path):
    runner = FakeRunner()
    rep = score_suite(_mk_suite(tmp_path), runner=runner, do_mutation=False,
                      label="t1")
    assert rep["detected_count"] == 2
    assert set(rep["detection"]) == {"D1", "D2", "D3", "D4", "D5"}
    assert rep["detection"]["D1"]["detected"] is True
    assert "test_d1.py::test_d1" in rep["detection"]["D1"]["tests"]
    assert rep["detection"]["D3"]["detected"] is False   # 假 runner 只会 D1/D2 败
    assert rep["n_tests"] == 2


def test_评分_检出明细不含decoy(tmp_path):
    rep = score_suite(_mk_suite(tmp_path), runner=FakeRunner(), do_mutation=False)
    assert "D6" not in rep["detection"] and "D7" not in rep["detection"]
