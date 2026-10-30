# -*- coding: utf-8 -*-
"""T014：E6/E7/E8 聚合（论文 6.8 表格数据源）。数字全部来自 json/SQL，可回溯。

用法：python scripts/aggregate_e68.py [--e7-min-task 181]
  --e7-min-task：E7 修复后批次下界（质检 P1-2 修复 tag exp-s2-20261031 后重跑=181-183）；
  修复前批次（166-168）单独列出并标"废弃口径（撒网虚高）"，供 6.9 素材引用。
输出：stdout markdown 表 + data/reports/aggregate_e68.md
"""
import glob
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "reports" / "aggregate_e68.md"


def agg_e6() -> list[str]:
    lines = ["### E6 真实项目（3 项目 × n=3）", "",
             "| 项目 | n | passed 均值 | failed 均值 | 覆盖率均值 | token 均值 | 任务 |",
             "|---|---|---|---|---|---|---|"]
    by = {}
    for f in sorted(glob.glob(str(ROOT / "data/reports/e6_*_r*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        r = d.get("report") or {}
        by.setdefault(d["project"], []).append(
            (d["task_id"], r.get("passed") or 0, r.get("failed") or 0,
             (r.get("coverage_line") or 0) * 100, (d.get("llm") or {}).get("total_tokens", 0)))
    for p, rows in sorted(by.items()):
        n = len(rows)
        lines.append(
            f"| {p} | {n} | {sum(r[1] for r in rows)/n:.0f} | {sum(r[2] for r in rows)/n:.1f} "
            f"| {sum(r[3] for r in rows)/n:.1f}% | {sum(r[4] for r in rows)/n:.0f} "
            f"| {','.join('#' + str(r[0]) for r in rows)} |")
    return lines


def agg_e7(min_task: int) -> list[str]:
    lines = ["### E7 跨级溯源命中率（corpus-003）", "",
             "| 批次 | 任务 | n | 命中率均值±std | 明细 |", "|---|---|---|---|---|"]
    by = {"修复后（缺边不回退）": [], "修复前（撒网回退，废弃口径）": []}
    for f in sorted(glob.glob(str(ROOT / "data/corpus/corpus-003/e7_task_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        if d.get("status") != "ok":
            continue  # blocked/invalid 不计均值（协议纪律）
        batch = "修复后（缺边不回退）" if d["task_id"] >= min_task else "修复前（撒网回退，废弃口径）"
        by[batch].append((d["task_id"], d["hit_rate"], f"{d['hits']}/{d['defects_real']}"))
    for batch, rows in by.items():
        if not rows:
            continue
        rates = [r[1] for r in rows]
        std = statistics.stdev(rates) if len(rates) > 1 else 0.0
        detail = "; ".join(f"#{t}={h}({d})" for t, h, d in rows)
        lines.append(f"| {batch} | {','.join('#' + str(r[0]) for r in rows)} | {len(rows)} "
                     f"| {statistics.mean(rates):.3f}±{std:.2f} | {detail} |")
    lines.append("")
    lines.append("> 修复前后差值 = 撒网虚高的直接测量（体检 P1-2，tag exp-s2-20261031 前后）。")
    return lines


def agg_e8() -> list[str]:
    lines = ["### E8 四级接力（corpus-003，n=3）", "",
             "| 轮 | 任务 | L1 | L2 | 契约违背 | token |", "|---|---|---|---|---|---|"]
    for f in sorted(glob.glob(str(ROOT / "data/reports/e8_r*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        legs = {l["level"]: l for l in d.get("legs", [])}
        l1, l2 = legs.get("L1", {}), legs.get("L2", {})
        s2 = l2.get("summary")
        cv = json.loads(s2).get("contract_violations") if isinstance(s2, str) else \
            (s2 or {}).get("contract_violations")
        tok = (d.get("llm") or {}).get("total_tokens", 0)
        lines.append(f"| {d['round']} | #{d['task_id']} "
                     f"| {l1.get('status')},{l1.get('verdict')} "
                     f"| {l2.get('status')},{l2.get('verdict')} | {cv} | {tok} |")
    return lines


def main() -> int:
    min_task = 181
    if "--e7-min-task" in sys.argv:
        min_task = int(sys.argv[sys.argv.index("--e7-min-task") + 1])
    lines = ["# E6/E7/E8 聚合（生成于 " + __import__("time").strftime("%Y-%m-%d %H:%M") + "）", ""]
    lines += agg_e6() + [""] + agg_e7(min_task) + [""] + agg_e8()
    text = "\n".join(lines)
    print(text)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"\n[agg] 已落 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
