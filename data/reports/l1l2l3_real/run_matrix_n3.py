# -*- coding: utf-8 -*-
"""件 3：六语言对照 n=3（2026-11-19 遗憾清偿设计，预注册协议执行）。

六语言 × 同需求（逐字复用基线 #1155-1160）× 3 次独立采样 = 18 格。
报告口径（预注册）：每格 mean±std（token/覆盖率/用例数）；终态报 3 中分布；
结论措辞限定"方向一致"而非"显著提升"。驱动器 v2（driver_kit：点火自检+
响应码校验）。成本预估 ~¥0.6（18×~16k tok）。
"""
import json
import statistics
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "data/reports/e10"))           # driver_kit 所在（件 5 公共层）
sys.path.insert(0, str(ROOT))

from driver_kit import create_and_run, ensure_docker, ensure_server  # noqa: E402

BASE = "http://127.0.0.1:8000"
OUT = ROOT / "data/reports/l1l2l3_real/matrix_n3_results.json"
TERMINAL = {"done", "failed", "cancelled"}
N_SAMPLES = 3

CELLS = json.loads((ROOT / "data/reports/l1l2l3_real/matrix_cells.json").read_text(encoding="utf-8"))


def db_metrics(tid: int) -> dict:
    import sqlite3
    conn = sqlite3.connect(f"file:{ROOT / 'data/test_agent.db' .as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        lc = conn.execute("SELECT COALESCE(SUM(total_tokens),0) tok FROM llm_calls WHERE task_id=?",
                          (tid,)).fetchone()
        ev = conn.execute("""SELECT coverage_line, verdict, failure_kind,
                             substr(COALESCE(stop_reason,''),1,60) sr FROM evaluation_rounds
                             WHERE task_id=? ORDER BY round_no DESC LIMIT 1""", (tid,)).fetchone()
        cases = conn.execute("""SELECT COUNT(*) n FROM test_cases c
                                JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=?""",
                             (tid,)).fetchone()
        return {"tokens": lc["tok"], "coverage": ev["coverage_line"] if ev else None,
                "verdict": ev["verdict"] if ev else None,
                "kind": ev["failure_kind"] if ev else None,
                "cases": cases["n"], "stop_reason": ev["sr"] if ev else ""}
    finally:
        conn.close()


def main() -> int:
    token = ensure_server(BASE)
    ensure_docker()
    s = requests.Session()
    h = {"Authorization": "Bearer " + token}
    recs = []
    total = len(CELLS) * N_SAMPLES
    i = 0
    for cell in CELLS:
        for rep in range(1, N_SAMPLES + 1):
            i += 1
            out = create_and_run(s, h, BASE, {"path": cell["path"],
                                              "requirement": cell["requirement"]})
            recs.append({"lang": cell["lang"], "rep": rep, "tid": out["tid"],
                         "steps": out["steps"], "status": "queued"})
            print(f"[n3 {i}/{total}] [{cell['lang']} r{rep}] → #{out['tid']}（"
                  + "、".join(f"{x['step']}={x['http']}" for x in out["steps"]) + "）", flush=True)

    t0, last = time.time(), ""
    while time.time() - t0 < 5 * 3600:
        states = []
        for rec in recs:
            d = s.get(f"{BASE}/api/tasks/{rec['tid']}", headers=h, timeout=10).json()
            rec["status"] = d.get("status")
            states.append(f"{rec['tid']}:{rec['status']}")
        line = " ".join(states)
        if line != last:
            print(f"[{int((time.time()-t0)/60):>3}m] {line}", flush=True)
            last = line
        if all(r["status"] in TERMINAL for r in recs):
            break
        time.sleep(40)

    for rec in recs:
        rec["metrics"] = db_metrics(rec["tid"])

    # 聚合：每语言 mean±std + 终态分布
    agg = {}
    for cell in CELLS:
        lang = cell["lang"]
        ms = [r["metrics"] for r in recs if r["lang"] == lang]
        toks = [m["tokens"] for m in ms]
        covs = [m["coverage"] for m in ms if m["coverage"] is not None]
        cases = [m["cases"] for m in ms]
        verdicts = [f"{m['verdict']}/{(m['kind'] or '')[:12]}" for m in ms]
        agg[lang] = {
            "tids": [r["tid"] for r in recs if r["lang"] == lang],
            "tokens_mean_std": [round(statistics.mean(toks)), round(statistics.pstdev(toks))],
            "coverage_list": covs,
            "cases_mean": round(statistics.mean(cases), 1),
            "verdicts": verdicts,
            "stop_reasons": [m["stop_reason"][:50] for m in ms],
        }
    doc = {"generated_at": time.strftime("%F %T"), "n_samples": N_SAMPLES,
           "protocol": "预注册 docs/遗憾清偿设计-20261119.md 件 3",
           "token_total": sum(r["metrics"]["tokens"] for r in recs),
           "aggregate": agg,
           "cells": recs}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[n3] 总 token {doc['token_total']} ≈ ¥{doc['token_total']/1e6*2:.2f}", flush=True)
    for lang, a in agg.items():
        print(f"  [{lang:>10}] tok {a['tokens_mean_std'][0]}±{a['tokens_mean_std'][1]} "
              f"覆盖 {a['coverage_list']} 用例≈{a['cases_mean']} 终态 {a['verdicts']}", flush=True)
    print(f"[n3] → {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
