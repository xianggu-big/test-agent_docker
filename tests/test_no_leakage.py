# -*- coding: utf-8 -*-
"""三道泄漏门禁（2026-11-23 外部 AI 协同定稿 Q5，pytest 化）。

背景：BugsInPy 零 issue 自主发现实验的判据是 fail-to-pass——staging 的 fixed
目录躺着【官方失败用例】，任何检索/上下文注入组件碰到它=patch leakage（审稿
一票否决）。三道门禁：
  ① harness_card 输出零泄漏断言（fixed 路径/官方测试函数名不进卡）
  ② 检索模块源码静态断言（harness_card.py 不得引用 fixed/test.patch/healthcard 路径模式）
  ③ prompt 注入端到端断言（组装后的提示词文本不含 fixed 路径与官方测试名）
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HC = ROOT / "scripts" / "harness_card.py"
STG = ROOT / "data/benchmarks/staging"


def _official_test_funcs(bug_dir: Path) -> set[str]:
    """官方失败用例的函数名集合（从 manifest 的 official_test_cmd 提取 nodeid 尾段）。"""
    man = ROOT / "data/benchmarks/manifests" / f"py-{bug_dir.name.split('-', 1)[1]}.json"
    if not man.exists():
        return set()
    m = json.loads(man.read_text(encoding="utf-8"))
    cmd = m.get("official_test_cmd", "")
    if "::" not in cmd:
        return set()
    return {tok.split("::")[-1].split("[")[0] for tok in cmd.split() if "::" in tok}


def test_harness_card_output_has_no_fixed_leakage(tmp_path):
    """门禁①：对 buggy 快照抽卡，输出 JSON 不得含 /fixed/ 路径或官方测试函数名。"""
    repo = STG / "bugsinpy-tqdm-bug1" / "buggy"
    if not repo.exists():
        pytest.skip("staging 不在（复跑 stage 生成）")
    out_p = tmp_path / "hc.json"
    r = subprocess.run([sys.executable, "-X", "utf8", "-I", str(HC),
                        str(repo), "--json-out", str(out_p)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    raw = out_p.read_text(encoding="utf-8")
    assert "/fixed/" not in raw, "线束卡输出含 fixed 路径=泄漏"
    official = _official_test_funcs(STG / "bugsinpy-tqdm-bug1")
    for fn in official:
        assert fn not in raw, f"线束卡输出含官方测试函数名 {fn}=泄漏"


def test_harness_card_source_never_reads_fixed_or_healthcard():
    """门禁②（AST 级静态断言）：抽取器源码不得出现 fixed/healthcard/test.patch
    路径模式（防未来有人往检索器里加这些来源）。"""
    src = HC.read_text(encoding="utf-8")
    import ast as _ast
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            v = node.value.lower()
            assert "healthcard" not in v, "抽取器引用 healthcard=泄漏面"
            assert "/fixed/" not in v, "抽取器硬编码 fixed 路径=泄漏面"
            assert "test.patch" not in v, "抽取器引用官方补丁=泄漏面"


def test_prompt_assembly_no_leakage_canary():
    """门禁③（端到端金丝雀）：模拟注入组装——卡+排除表渲染成提示词段后，
    依然不含 fixed 路径与官方测试名。这是 ①② 的组合防线（真注入管线
    接线后把组装函数 import 进来替换此模拟）。"""
    repo = STG / "bugsinpy-tqdm-bug1" / "buggy"
    if not repo.exists():
        pytest.skip("staging 不在")
    sys.path.insert(0, str(ROOT / "scripts"))
    from harness_card import extract_card   # noqa: E402
    out = extract_card(repo)
    segment = json.dumps(out, ensure_ascii=False) + "\n" + "\n".join(out["names_excluded"])
    assert "/fixed/" not in segment
    official = _official_test_funcs(STG / "bugsinpy-tqdm-bug1")
    # 排除表是 buggy 侧自带测试名——官方新增函数天然不在其中；若在=staging 污染
    for fn in official:
        assert fn not in segment, f"官方测试名 {fn} 渗入排除表=staging 污染"
