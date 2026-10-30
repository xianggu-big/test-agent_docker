# -*- coding: utf-8 -*-
"""三角色对比实验聚合：30 份 roleexp_*.json → 对比报告骨架（机器聚合+人工复核栏）。
用法：python scripts/aggregate_roleexp.py → docs/角色对比实验-报告-20261031.md
"""
import glob
import json
import sys
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "角色对比实验-报告-20261031.md"
ROLES = ["小白", "初级", "高级"]


def main() -> int:
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/reports/roleexp_*.json"))):
        rows.append(json.load(open(f, encoding="utf-8")))
    by = defaultdict(list)
    for r in rows:
        by[r["role"]].append(r)

    lines = ["# 三角色对比实验报告（生成于 " + __import__("time").strftime("%Y-%m-%d %H:%M") + "）", "",
             "> 被测：tenacity（82.6KB，当前能力边界内最大）；每角色 10 题需求端到端走图。",
             "> 口径：假失败=verdict 为 stop 且 stop_reason 含『疑似产品缺陷』但人工复核为预言错误的任务；",
             "> 本骨架的假失败列为**机器初判**（stop+product 措辞计数），逐题人工复核结论填在复核栏。", ""]
    lines += ["## 1. 角色级聚合", "",
              "| 角色 | 轮数 | 终态可判 | pass | stop | failed | 异常 | passed 均值 | 覆盖率均值 | token 均值 | 机器初判假失败 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for role in ROLES:
        rs = by.get(role, [])
        fin = [r for r in rs if r.get("final")]
        verdicts = [r["final"]["verdict"] for r in fin]
        pf = [r["final"].get("stop_reason", "") for r in fin]
        fake = sum(1 for r in fin if r["final"]["verdict"] == "stop"
                   and "疑似产品缺陷" in (r["final"].get("stop_reason") or ""))
        covs = [r["final"].get("coverage_line") or 0 for r in fin]
        toks = [(r.get("llm") or {}).get("total_tokens", 0) for r in rs]
        lines.append(
            f"| {role} | {len(rs)} | {len(fin)} | {verdicts.count('pass')} | {verdicts.count('stop')} "
            f"| {verdicts.count('failed')} | {len(rs) - len(fin)} "
            f"| {sum(r['final'].get('passed') or 0 for r in fin)/len(fin):.0f} "
            f"| {sum(covs)/len(fin)*100:.1f}% | {sum(toks)/len(rs):.0f} | {fake} |" if fin else
            f"| {role} | {len(rs)} | 0 | — | — | — | {len(rs)} | — | — | {sum(toks)/len(rs):.0f} | — |")
    lines += ["", "## 2. 逐题明细", ""]
    for role in ROLES:
        lines += [f"### {role}", "",
                  "| 题 | 任务 | 终态 | passed/failed | 覆盖率 | stop_reason（截 60） | 人工复核（假失败/真缺陷/场景问题） |",
                  "|---|---|---|---|---|---|---|"]
        for r in sorted(by.get(role, []), key=lambda x: x["question"]):
            fin = r.get("final") or {}
            lines.append(
                f"| {r['question']} | #{r['task_id']} | {fin.get('verdict', 'EXC')} "
                f"| {fin.get('passed')}/{fin.get('failed')} "
                f"| {round((fin.get('coverage_line') or 0)*100)}% "
                f"| {(fin.get('stop_reason') or '')[:60]} | （待填） |")
        lines.append("")
    lines += ["## 3. 对比结论（人工复核后填写）", "",
              "1. 需求表达质量 vs 生成用例数/覆盖：", "2. 假失败率随角色专业度的变化：",
              "3. 高级找茬题（参数误用/时序/并发）的暴露能力：", "4. 对 6.9 需求侧维度的支撑："]
    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"[agg] {len(rows)} 份 json 聚合 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
