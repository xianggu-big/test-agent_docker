# -*- coding: utf-8 -*-
"""agentloop_ab_analysis.py——A5b 循环开/关对照臂的判读脚本（预注册判读 G1-G4）。

判读口径（跑前定死，2026-11-26 总包 A5b，不许改）：
  G1 覆盖率：难项目（corpus-003）上 开−关 ≥ +5pp 且 bootstrap 95% CI 不含 0 才写
     "有增益"；演示项目只报持平与否。
  G2 检出：难项目植入缺陷检出数（run_g_scoring.py 反事实差分口径）开 vs 关，
     报方向与 CI。
  G3 广度：用例数/格、失败数/格、无依据失败占比（expected_basis=常识/模型推断
     且需求 2-gram 无交叠）。
  G4 成本：token/格 与 P50 的增幅。
  n<30/臂 一律标"方向性读数"；任何一项不达标，结论写"循环增益无证据"。

臂别真源：结算件 scale_{on|off}_*.json 的 arm_loop_enabled（=run 行
config_json.flags.agent_loop_enabled），分析按臂分不按任务号段。

用法：python -I scripts/agentloop_ab_analysis.py
产出：stdout 判读表 + data/reports/agentloop_scale/ab_judgment.json
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CELLS = ROOT / "data" / "reports" / "agentloop_scale" / "cells"
PROD_DB = ROOT / "data" / "test_agent.db"
OUT = ROOT / "data" / "reports" / "agentloop_scale" / "ab_judgment.json"


def _conn():
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_arm(tag: str) -> list[dict]:
    out = []
    for p in sorted(CELLS.glob(f"scale_{tag}_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_file"] = p.name
        out.append(d)
    return out


def boot_ci(values: list[float], stat="mean", n_boot: int = 10000) -> tuple[float, float]:
    """bootstrap 95% CI（seed 固定=可复现）；样本<2 或全同 → (x, x) 如实退化。"""
    if len(values) < 2:
        v = values[0] if values else 0.0
        return (v, v)
    rng = random.Random(0)
    stats = []
    for _ in range(n_boot):
        s = [values[rng.randrange(len(values))] for _ in values]
        stats.append(sum(s) / len(s))
    stats.sort()
    return (stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats))])


def per_cell_g3(conn, task_id: int, req_text: str) -> dict:
    """G3 三件：用例数（执行过的用例结果行）、失败数、无依据失败占比。
    test_cases 无 task_id 列——经 test_scripts.script_id 关联（schema 实测）。"""
    rows = conn.execute(
        "SELECT nodeid, status FROM test_case_results WHERE task_id=?",
        (task_id,)).fetchall()
    n_cases = len(rows)
    failed = [r for r in rows if r["status"] == "failed"]
    n_unfounded = 0
    for f in failed:
        name = (f["nodeid"] or "").split("::")[-1]
        case = conn.execute(
            "SELECT c.expected_basis, c.expected FROM test_cases c "
            "JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=? AND c.name=? "
            "ORDER BY c.id DESC LIMIT 1", (task_id, name)).fetchone()
        if case is None:
            # nodeid 与 case name 可能带后缀——退化为前缀匹配
            case = conn.execute(
                "SELECT c.expected_basis, c.expected FROM test_cases c "
                "JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=? "
                "AND ? LIKE c.name || '%'", (task_id, name)).fetchone()
        basis = (case["expected_basis"] if case else None) or ""
        if ("常识" in basis or "模型推断" in basis or "推断" in basis):
            expected = (case["expected"] if case else "") or ""
            if not _req_supports(expected, req_text):
                n_unfounded += 1
    return {"n_cases": n_cases, "n_failed": len(failed),
            "n_unfounded_failed": n_unfounded,
            "unfounded_rate": (n_unfounded / len(failed)) if failed else None}


def _req_supports(expected: str, req_text: str, k: int = 2) -> bool:
    """需求 2-gram 交叠（复用 gen_tests 拼接口径思想）：expected 里任一 k-gram
    出现在需求原文=有依据线索。中文按字符、英文按词。"""
    import re
    exp = expected or ""
    grams: list[str] = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", exp):
        grams.append(tok.lower())
    for tok in re.findall(r"[\u4e00-\u9fff]{2,}", exp):
        for i in range(len(tok) - k + 1):
            grams.append(tok[i:i + k])
    if not grams:
        return False
    req_low = (req_text or "").lower()
    return any(g.lower() in req_low for g in grams)


def main() -> int:
    conn = _conn()
    on_cells = load_arm("on")
    off_cells = load_arm("off")
    # 臂别真源=config_json.flags.agent_loop_enabled；tag 误挂的格按真源重新归臂
    # （纪律原文：分析按 config_json.flags 分臂，不按任务号段/文件名）
    reassigned = []
    all_cells = [("on", on_cells), ("off", off_cells)]
    true_on, true_off = [], []
    for tag, cells in all_cells:
        for c in cells:
            if c.get("arm_loop_enabled") is True:
                true_on.append(c)
                if tag != "on":
                    reassigned.append({"file": c["_file"], "tag": tag, "true_arm": "on"})
            elif c.get("arm_loop_enabled") is False:
                true_off.append(c)
                if tag != "off":
                    reassigned.append({"file": c["_file"], "tag": tag, "true_arm": "off"})
    on_cells, off_cells = true_on, true_off
    out: dict = {"n_on": len(on_cells), "n_off": len(off_cells),
                 "reassigned_by_arm_truth": reassigned}

    # 臂别断言（真源=config_json.flags）
    for tag, cells in (("on", on_cells), ("off", off_cells)):
        for c in cells:
            expect = True if tag == "on" else False
            if c.get("arm_loop_enabled") is not expect:
                print(f"[ARM-MISMATCH] {c['_file']} arm_loop_enabled="
                      f"{c.get('arm_loop_enabled')} 但 tag={tag}")
    req_corpus = (ROOT / "data/corpus/corpus-003/project/REQUIREMENT.md").read_text(
        encoding="utf-8")

    # ---- G1 覆盖率 ----
    def cov_by(cells, proj):
        return [c["coverage_line"] for c in cells
                if c["key"] == proj and c.get("coverage_line") is not None]

    g1 = {}
    for proj in ("corpus-003", "js-calc", "go-api-book", "coupon-api"):
        co, cf = cov_by(on_cells, proj), cov_by(off_cells, proj)
        if not co or not cf:
            g1[proj] = {"n_on": len(co), "n_off": len(cf), "note": "样本不足"}
            continue
        diff = sum(co) / len(co) - sum(cf) / len(cf)
        lo1, hi1 = boot_ci(co)
        lo0, hi0 = boot_ci(cf)
        # 差值的 CI：独立样本均值差（保守：两臂 CI 区间差的不重叠性另报）
        rng = random.Random(0)
        diffs = []
        for _ in range(10000):
            a = [co[rng.randrange(len(co))] for _ in co]
            b = [cf[rng.randrange(len(cf))] for _ in cf]
            diffs.append(sum(a) / len(a) - sum(b) / len(b))
        diffs.sort()
        ci = (diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))])
        g1[proj] = {"on_mean": round(sum(co) / len(co), 4),
                    "off_mean": round(sum(cf) / len(cf), 4),
                    "diff": round(diff, 4), "diff_CI95": [round(ci[0], 4), round(ci[1], 4)],
                    "on_CI95": [round(lo1, 4), round(hi1, 4)],
                    "off_CI95": [round(lo0, 4), round(hi0, 4)],
                    "gain_claim": bool(diff >= 0.05 and (ci[0] > 0 or ci[1] < 0))}
    out["G1"] = g1

    # ---- G3 广度 ----
    g3 = {}
    for tag, cells in (("on", on_cells), ("off", off_cells)):
        per = [per_cell_g3(conn, c["task_id"], req_corpus) for c in cells]
        n_c = [p["n_cases"] for p in per]
        n_f = [p["n_failed"] for p in per]
        n_u = [p["n_unfounded_failed"] for p in per]
        tot_f = sum(n_f)
        g3[tag] = {"cases_per_cell": round(sum(n_c) / len(n_c), 2) if n_c else None,
                   "failed_per_cell": round(sum(n_f) / len(n_f), 2) if n_f else None,
                   "unfounded_failed_total": sum(n_u),
                   "failed_total": tot_f,
                   "unfounded_share": round(sum(n_u) / tot_f, 4) if tot_f else None,
                   "per_cell": per}
    out["G3"] = g3

    # ---- G4 成本 ----
    g4 = {}
    for tag, cells in (("on", on_cells), ("off", off_cells)):
        toks = []
        durs = []
        for c in cells:
            r = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t FROM llm_calls "
                "WHERE task_id=?", (c["task_id"],)).fetchone()
            toks.append(r["t"])
            rr = conn.execute("SELECT started_ms, ended_ms FROM agent_runs WHERE task_id=?",
                              (c["task_id"],)).fetchall()
            for x in rr:
                if x["started_ms"] and x["ended_ms"]:
                    durs.append((x["ended_ms"] - x["started_ms"]) / 1000)
        toks.sort(); durs.sort()
        g4[tag] = {"token_per_cell": round(sum(toks) / len(toks), 0) if toks else None,
                   "cost_yuan_per_cell": round(
                       sum(c.get("cost_yuan_est") or 0 for c in cells) / len(cells), 4)
                       if cells else None,
                   "P50_s": round(durs[len(durs) // 2], 1) if durs else None}
    if g4.get("on", {}).get("token_per_cell") and g4["on"]["token_per_cell"]:
        base = g4["off"]["token_per_cell"] or 0
        g4["token_growth"] = (g4["on"]["token_per_cell"] / base - 1) if base else None
        base50 = g4["off"]["P50_s"] or 0
        g4["P50_growth_s"] = (g4["on"]["P50_s"] - base50) if base50 else None
    out["G4"] = g4

    # ---- G2 检出（反事实差分）——需要套件目录，由 run_g_scoring 单独跑，这里只聚合 ----
    g2_file = CELLS.parent / "ab_g2_scores.json"
    if g2_file.exists():
        out["G2"] = json.loads(g2_file.read_text(encoding="utf-8"))
    else:
        out["G2"] = {"note": "尚未跑 run_g_scoring（G2 在套件落档后由 scoring 脚本回填）"}

    out["directional_label"] = "方向性读数" if (len(on_cells) < 30 or len(off_cells) < 30) \
        else "n>=30"
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    slim = {k: (v if k not in ("G3",) else {t: {kk: vv for kk, vv in d.items()
                                                if kk != "per_cell"}
                                            for t, d in v.items()})
            for k, v in out.items()}
    print(json.dumps(slim, ensure_ascii=False, indent=1))
    print(f"\n[written] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
