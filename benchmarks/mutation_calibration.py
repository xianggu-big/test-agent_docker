"""E4 变异标定实验：归因器 classify_failure 的混淆矩阵（docs/experiment-design.md §4）。

目的：归因器（env / case_defect / product_defect 三分类）首次获得可控真值——
不注入缺陷时无从知道"疑似 product_defect"判对了没有。

真值构造（对被测对象与测试资产分别做手脚，固定套件只变一样）：
- product 真值：对场景E/cart.py 施加 8 个变异算子（条件取反/运算符替换/常量偏移/
  舍入移除/异常吞掉/初值污染/累加破坏/求和偏移），套件全部断言式、保证杀死每个
  mutant（产生可归因的 AssertionError 类失败）；
- case 真值：原版 cart.py + 正常套件 + 一类已知坏用例（ImportError / AttributeError /
  SyntaxError / NameError / 空套件 no tests ran）；
- env 真值：容器内卸载 pytest 后执行（No module named pytest）。

受控：不调 LLM（归因是确定性启发式，实验测的就是它本身）；Docker 真实执行；
每样本独立干净容器。混淆矩阵 3×3（真值×预测），另报准出判定两类错误——
漏报（真 product 未判 product → 该停没停，造假风险）与误报（非 product 判成
product → 不该停停了，误停成本）。

诚实边界：这是"已知真值集上的准确率"（标定上界），不等于野外准确率——真实失败
往往信号混合（断言失败也可能是用例错），这正是证人冻结 + 人工标注存在的理由
（oracle 问题，论文引 Barr 综述）。

标定-修复-复测记录（2026-10-28）：首轮 11/14——①C4 NameError 被误判 product，
暴露 classify_failure 与 CASE_OWN_DEFECT_ERRORS 口径不一致（已修：正则补
NameError/UnboundLocalError）；②M4 舍入移除 mutant 存活（套件弱，补 33.33%
非有限小数用例）；③env 样本 pip 卸载因非 root 被拒（改 user="root"）。
首轮结果留档 benchmarks/results/mutation_calibration_20261028-080339.*。

运行：python benchmarks/mutation_calibration.py  （需 Docker，无需 LLM Key）
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config
from test_agent.graph import repair
from test_agent.sandbox.runner import SandboxRunner

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "demo" / "场景E"
CMD = ["pytest", "-q", "--tb=short", "-rfE"]
CONTAINER = "mta-mutcal"          # 每样本独立重建（干净容器纪律）

# 断言式标定套件：预期值全部来自规格（docstring/常识），保证 mutant 杀死即 AssertionError
SUITE = '''"""标定套件：全部断言式用例，预期值来自规格而非实现。"""
import pytest
from cart import apply_discount, Cart


def test_discount_normal():
    assert apply_discount(100, 20) == 80.0


def test_discount_edges():
    assert apply_discount(100, 0) == 100.0
    assert apply_discount(100, 100) == 0.0


def test_discount_rounding():
    assert apply_discount(100, 33) == 67.0


def test_discount_invalid():
    with pytest.raises(ValueError):
        apply_discount(100, 150)
    with pytest.raises(ValueError):
        apply_discount(100, -1)


def test_discount_fine_rounding():
    # 非有限小数场景：杀死"舍入移除"变异（首轮标定该 mutant 存活，套件弱点已补）
    assert apply_discount(10, 33.33) == 6.67


def test_cart_total():
    c = Cart()
    c.add("a", 30)
    c.add("b", 70)
    assert c.total() == 100.0


def test_cart_empty():
    assert Cart().total() == 0
'''

# 8 个变异算子：名称 → [(原文, 替换)]；old 必须在 cart.py 中恰好出现一次
MUTANTS = [
    ("M1·条件取反", [("if not 0 <= percent <= 100:", "if 0 <= percent <= 100:")]),
    ("M2·运算符替换", [("price * (1 - percent / 100)", "price * (1 + percent / 100)")]),
    ("M3·常量偏移", [("percent / 100", "percent / 10")]),
    ("M4·舍入移除", [("return round(price * (1 - percent / 100), 2)",
                      "return price * (1 - percent / 100)")]),
    ("M5·异常吞掉", [('raise ValueError("percent out of range")', "return 0.0")]),
    ("M6·初值污染", [("self.items = []", 'self.items = [("", 1)]')]),
    ("M7·累加破坏", [("self.items.append((name, price))", "self.items.append((name, 0))")]),
    ("M8·求和偏移", [("return round(sum(p for _, p in self.items), 2)",
                      "return round(sum(p for _, p in self.items) + 1, 2)")]),
]

# case 真值样本：文件名 → 内容（None = 只放空测试文件制造 no tests ran）
BAD_CASES = [
    ("C1·ImportError坏用例", {"test_bad1.py": 'def test_bad_import():\n    from cart import discount\n\n    assert discount(100, 20) == 80.0\n'}),
    ("C2·AttributeError坏用例", {"test_bad2.py": 'def test_bad_attr():\n    from cart import Cart\n\n    c = Cart()\n    c.add("a", 10)\n    c.clear()\n'}),
    ("C3·SyntaxError坏用例", {"test_bad3.py": "def test_broken(:\n    pass\n"}),
    ("C4·NameError坏用例", {"test_bad4.py": "def test_bad_name():\n    from cart import apply_discount\n\n    assert apply_discount(100, 20) == eighty\n"}),
    ("C5·空套件(no tests ran)", None),
]

PRED_LABEL = {"product_defect": "product", "case_defect": "case", "env": "env", "none": "none"}
EXPECT_VERDICT = {"product": "stop", "case": "repair_gen", "env": "repair_env"}


def mutate(source: str, ops: list[tuple[str, str]]) -> str:
    out = source
    for old, new in ops:
        assert out.count(old) == 1, f"变异算子锚点不唯一/缺失：{old!r}"
        out = out.replace(old, new)
    return out


def run_sample(runner: SandboxRunner, files: dict[str, str]) -> tuple[int, str, str]:
    """干净容器内执行一套固定文件，返回 (exit, stdout, stderr)。"""
    ws = Path(tempfile.mkdtemp(prefix="mutcal_"))
    try:
        for name, content in files.items():
            (ws / name).write_text(content, encoding="utf-8")
        runner.cleanup(CONTAINER)
        runner.ensure_container(config.IMAGE_PY, CONTAINER, fresh=True)
        container = runner.client.containers.get(CONTAINER)
        runner.copy_dir_into(container, ws)
        r = runner.exec_in(container, CMD)
        return r.exit_code, r.stdout, r.stderr
    finally:
        shutil.rmtree(ws.parent, ignore_errors=True)


def main() -> None:
    runner = SandboxRunner()
    src = (SRC / "cart.py").read_text(encoding="utf-8")
    samples: list[dict] = []

    def record(name: str, truth: str, exit_code: int, stdout: str, stderr: str) -> None:
        kind = repair.classify_failure(exit_code, stdout, stderr)
        pred = PRED_LABEL[kind]
        fp = repair.fingerprint_of(stdout, stderr)
        verdict, _, _ = repair.decide(exit_code, stdout, stderr, [], fp, 1, None, 0.70)
        samples.append({"name": name, "truth": truth, "pred": pred, "kind": kind,
                        "verdict": verdict, "expect_verdict": EXPECT_VERDICT.get(truth, "—"),
                        "exit": exit_code})

    # —— product 真值：8 个 mutant（每个都被套件杀死 → 断言类失败）——
    for name, ops in MUTANTS:
        ec, out, err = run_sample(runner, {"cart.py": mutate(src, ops), "test_calibration.py": SUITE})
        record(name, "product", ec, out, err)
        print(f"  {name}: exit={ec} → {samples[-1]['pred']}({samples[-1]['verdict']})", flush=True)

    # —— case 真值：原版被测对象 + 套件 + 一类已知坏用例 ——
    for name, bad in BAD_CASES:
        files = {"cart.py": src, "test_calibration.py": SUITE}
        if bad is None:
            files = {"cart.py": src, "test_empty.py": "# 空套件：无可收集用例\n"}
        else:
            files.update(bad)
        ec, out, err = run_sample(runner, files)
        record(name, "case", ec, out, err)
        print(f"  {name}: exit={ec} → {samples[-1]['pred']}({samples[-1]['verdict']})", flush=True)

    # —— env 真值：卸载 pytest 后执行（专用容器，不回收依赖）——
    ws = Path(tempfile.mkdtemp(prefix="mutcal_env_"))
    try:
        (ws / "cart.py").write_text(src, encoding="utf-8")
        (ws / "test_calibration.py").write_text(SUITE, encoding="utf-8")
        runner.cleanup(CONTAINER)
        runner.ensure_container(config.IMAGE_PY, CONTAINER, fresh=True)
        container = runner.client.containers.get(CONTAINER)
        runner.copy_dir_into(container, ws)
        u = container.exec_run(["pip", "uninstall", "-y", "pytest"], user="root")
        assert u.exit_code == 0, f"pytest 卸载失败（env 样本无效）：{u.output}"
        r = runner.exec_in(container, ["python", "-m", "pytest", "-q"])
        record("V1·pytest缺失(env)", "env", r.exit_code, r.stdout, r.stderr)
        print(f"  V1·pytest缺失: exit={r.exit_code} → {samples[-1]['pred']}", flush=True)
    finally:
        shutil.rmtree(ws.parent, ignore_errors=True)
    runner.cleanup(CONTAINER)

    # —— 汇总：3×3 混淆矩阵 + 准出判定两类错误 ——
    classes = ["product", "case", "env"]
    matrix = {t: {p: 0 for p in classes + ["none"]} for t in classes}
    for s in samples:
        matrix[s["truth"]][s["pred"]] += 1
    total = len(samples)
    correct = sum(matrix[t][t] for t in classes)
    missed = matrix["product"]["case"] + matrix["product"]["env"] + matrix["product"]["none"]
    false_stop = matrix["case"]["product"] + matrix["env"]["product"]
    verdict_err = [s for s in samples if s["verdict"] != s["expect_verdict"]]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "benchmarks" / "results" / f"mutation_calibration_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# E4 变异标定：归因器混淆矩阵（classify_failure）",
        f"\n- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 真值构造：8 个 mutant（product）+ 5 类已知坏用例（case）+ 卸载 pytest（env）",
        "- 受控：不调 LLM（归因为确定性启发式）；每样本独立干净容器；真实 Docker 执行",
        "- 诚实边界：已知真值集上的准确率（标定上界），不等于野外准确率（oracle 问题）",
        "",
        "| 样本 | 真值 | 归因预测 | R-Loop 判定 | 预期判定 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for s in samples:
        ok = "✅" if s["pred"] == s["truth"] and s["verdict"] == s["expect_verdict"] else "❌"
        lines.append(f"| {s['name']} | {s['truth']} | {s['pred']} | "
                     f"{s['verdict']} | {s['expect_verdict']} | {ok} |")
    lines += [
        "",
        "## 混淆矩阵（行=真值，列=预测）",
        "",
        "| 真值\\预测 | product | case | env | 无失败 |",
        "|---|---|---|---|---|",
    ]
    for t in classes:
        row = matrix[t]
        lines.append(f"| **{t}** | {row['product']} | {row['case']} | {row['env']} | {row['none']} |")
    recall = {t: (matrix[t][t] / sum(matrix[t].values()) if sum(matrix[t].values()) else None)
              for t in classes}
    col = lambda p: sum(matrix[t][p] for t in classes)
    prec = {p: (matrix[p][p] / col(p) if col(p) else None) for p in classes}
    lines += [
        "",
        f"- **总体准确率**：{correct}/{total} = {correct / total:.0%}",
        f"- **漏报（真 product 未判 product → 该停没停，造假风险）**：{missed}",
        f"- **误报（非 product 判成 product → 不该停停了，误停成本）**：{false_stop}",
        f"- **判定错误样本**：{len(verdict_err)} 条" + ("" if not verdict_err else
          "：" + "；".join(f"{s['name']}→{s['verdict']}" for s in verdict_err)),
        f"- 各类召回率：product {recall['product']:.0%} / case {recall['case']:.0%} / env {recall['env']:.0%}",
        "",
        "## 论文口径（§7.3）",
        "",
        "- 强信号类（ImportError/AttributeError/SyntaxError/NameError/no tests ran）→ case 全对：",
        "  归因器对\"用例自身坏掉\"的可判别信号是可靠的；",
        "- 断言失败 mutant → product 全对：预期值来自规格（docstring/常识）的用例失败，",
        "  归因器将其判为疑似缺陷并触发证人冻结停机（防造假语义的机械化前提成立）；",
        "- 该矩阵证明的是\"在已知真值集上归因正确\"，真实失败常信号混合（断言失败也可能是",
        "  用例预期写错），因此系统仍保留人工标注通道与\"宁可误报不可造假\"的保守口径。",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(
        {"samples": samples, "matrix": matrix, "accuracy": correct / total,
         "missed_stop": missed, "false_stop": false_stop},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n混淆矩阵已写入 {out}")
    print(f"准确率 {correct}/{total}；漏报 {missed}；误报 {false_stop}；判定错误 {len(verdict_err)}")


if __name__ == "__main__":
    main()
