# -*- coding: utf-8 -*-
"""E9/E10 波末聚合与进度报告（只读库 + 读留档 JSON，不改实验数字）。

产出：
- 控制台摘要（各 Arm 完成数/累计 token/失败率/剩余量）
- data/reports/e9e10_progress_<date>.md 进度报告
- R1 双价目分账（llm_calls 按 model 字段分账；价目表透明记录在报告内）
用法：python aggregate_wave.py [--e9|--e10|--all]
"""
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
E9 = ROOT / "data" / "reports" / "e9_full"
E10 = ROOT / "data" / "reports" / "e10_full"
DB = ROOT / "data" / "test_agent.db"

# 双价目（元/百万 token，透明记录；输入=缓存未命中口径）
PRICES = {"deepseek-chat": {"in": 2.0, "out": 8.0},
          "deepseek-reasoner": {"in": 4.0, "out": 16.0}}
TARGETS = {  # 任务包目标规模
    "e9": {"A1": 384, "A2": 90, "A3": 54, "A4": 120, "A5": 50},
    "e10": {"E10A1": 63, "E10A2": 30},  # A2 目标=双向 2×3 语料×n5（先例口径扩展）
}


def agg_dir(out: Path, plan_ids: dict[str, int]) -> dict:
    cells = defaultdict(lambda: {"done": 0, "other": 0, "tokens": 0, "fail": 0})
    for f in sorted(out.glob("*.json")):
        if f.name in ("k_curve.json", "arm1_corpus.json", "arm4_oracles.json",
                      "arm4_projects.json"):
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if r.get("status") in ("error", "abandoned_prev_wave", "budget_exhausted"):
            continue  # 未完成格：计划数-完成数 即剩余量
        arm = str(r.get("arm") or "")
        cells[arm]["done"] += 1
        cells[arm]["tokens"] += int(r.get("tokens_spent") or 0)
    rows = {}
    for arm, target in plan_ids.items():
        c = cells.get(arm, {"done": 0, "tokens": 0})
        rows[arm] = {"done": c["done"], "target": target,
                     "remain": max(0, target - c["done"]),
                     "tokens": c["tokens"],
                     "pct": round(100 * c["done"] / target, 1) if target else None}
    return rows


def model_split() -> dict:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT model, COUNT(*) n, COALESCE(SUM(prompt_tokens),0) pt,"
        " COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(total_tokens),0) tt"
        " FROM llm_calls GROUP BY model").fetchall()
    conn.close()
    out = {}
    cost = 0.0
    for r in rows:
        p = PRICES.get(r["model"] or "deepseek-chat", PRICES["deepseek-chat"])
        c = (r["pt"] / 1e6) * p["in"] + (r["ct"] / 1e6) * p["out"]
        cost += c
        out[r["model"] or "?"] = {"calls": r["n"], "prompt": r["pt"],
                                  "completion": r["ct"], "total": r["tt"],
                                  "cost_cny": round(c, 2)}
    return {"by_model": out, "cost_cny_est": round(cost, 2)}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "--all"
    report = [f"# E9/E10 波次进度报告（{time.strftime('%F %T')}）\n"]
    if which in ("--e9", "--all"):
        rows = agg_dir(E9, TARGETS["e9"])
        total_tok = sum(r["tokens"] for r in rows.values())
        total_done = sum(r["done"] for r in rows.values())
        total_tgt = sum(r["target"] for r in rows.values())
        report += ["## E9 规模化普查", "",
                   "| Arm | 完成 | 目标 | 剩余 | 完成率 | 累计 token |",
                   "|---|---|---|---|---|---|"]
        for arm, r in rows.items():
            report.append(f"| {arm} | {r['done']} | {r['target']} | {r['remain']} "
                          f"| {r['pct']}% | {r['tokens']} |")
        report += ["", f"E9 合计：{total_done}/{total_tgt} 格，{total_tok} token\n"]
    if which in ("--e10", "--all"):
        rows = agg_dir(E10, TARGETS["e10"])
        report += ["## E10 深挖进化闭环", "",
                   "| Arm | 完成 | 目标 | 剩余 | 完成率 | 累计 token |",
                   "|---|---|---|---|---|---|"]
        for arm, r in rows.items():
            report.append(f"| {arm} | {r['done']} | {r['target']} | {r['remain']} "
                          f"| {r['pct']}% | {r['tokens']} |")
        report.append("")
    ms = model_split()
    report += ["## LLM 记账分账（llm_calls 按 model 字段，双价目换算）", "",
               "| model | 调用 | prompt | completion | total | 估算成本(元) |",
               "|---|---|---|---|---|---|"]
    for m, r in ms["by_model"].items():
        report.append(f"| {m} | {r['calls']} | {r['prompt']} | {r['completion']} "
                      f"| {r['total']} | {r['cost_cny']} |")
    report += ["", f"合计估算成本：{ms['cost_cny_est']} 元"
               f"（价目表：chat in 2/out 8，reasoner in 4/out 16 元每百万，缓存未命中口径）\n"]
    text = "\n".join(report)
    print(text)
    dest = ROOT / "data" / "reports" / f"e9e10_progress_{time.strftime('%Y%m%d_%H%M')}.md"
    dest.write_text(text, encoding="utf-8")
    print(f"[saved] {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
