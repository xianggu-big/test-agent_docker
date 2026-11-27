# -*- coding: utf-8 -*-
"""agentloop_phase0_judge.py——阶段 0（大纲证伪实验）判读脚本，零 API。

预注册卡：docs/任务卡-阶段0-大纲证伪实验-20261127.md（§3 格子与余量规则、§4 判据）。
判据口径全部来自该卡，本脚本只负责按卡计算，不做任何现场调整。

用法：
  python -I scripts/agentloop_phase0_judge.py --max-idx 5 [--score]
读 data/reports/agentloop_scale/cells/scale_p0{a,b}_<项目>_<idx>.json（idx ≤ max-idx）。
--score：对缺评分文件的语料格先跑反事实评分（run_g_scoring，零 LLM）。
产出：stdout 判读表 + data/reports/agentloop_scale/phase0_judgment_<max-idx>.json
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CARD = ROOT / "docs" / "任务卡-阶段0-大纲证伪实验-20261127.md"
CELLS = ROOT / "data" / "reports" / "agentloop_scale" / "cells"
DB = ROOT / "data" / "test_agent.db"
# 项目 → (角色, 语料编号或 None)
PROJECTS = {
    "js-calc": ("defect", None),
    "go-api-book": ("defect", None),
    "coupon-api": ("clean", None),
    "corpus-003": ("defect", "corpus-003"),
    "corpus-001": ("defect", "corpus-001"),     # 留出：执行者写大纲前未读其真值
}
HEADROOM_FULL, HEADROOM_NONE = 0.50, 0.80       # 卡 §3 余量规则
H1_PP, H2_FP_PP = 0.20, 0.20                    # 卡 §4 主判据与护栏


def _conn():
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def vague_requirement() -> str:
    """卡里的泛需求代码块（逐字），供"两臂同一把尺"的无依据口径使用。"""
    lines = CARD.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("**泛需求**"))
    a = next(i for i in range(start, len(lines)) if lines[i].startswith("```"))
    b = next(i for i in range(a + 1, len(lines)) if lines[i].startswith("```"))
    return chr(10).join(lines[a + 1:b]).strip()


def wilson(k: int, n: int, z: float = 1.959963985) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def boot_diff_ci(a_hits: list[int], b_hits: list[int], n_boot: int = 10000) -> list[float]:
    """B 减 A 的 bootstrap 95% CI（seed 固定）。"""
    rng = random.Random(0)
    diffs = []
    for _ in range(n_boot):
        sa = sum(a_hits[rng.randrange(len(a_hits))] for _ in a_hits) / len(a_hits)
        sb = sum(b_hits[rng.randrange(len(b_hits))] for _ in b_hits) / len(b_hits)
        diffs.append(sb - sa)
    diffs.sort()
    return [round(diffs[int(0.025 * n_boot)], 3), round(diffs[int(0.975 * n_boot) - 1], 3)]


def score_file(arm: str, corpus: str, tid: int) -> Path:
    return ROOT / "data" / "corpus" / corpus / "g_scores" / f"p0{arm}_t{tid}.json"


def ensure_score(arm: str, corpus: str, tid: int) -> dict | None:
    """缺评分文件就跑反事实评分（零 LLM）；失败如实返回 None。"""
    f = score_file(arm, corpus, tid)
    if not f.exists():
        ws = ROOT / "data" / "workspaces" / f"task_{tid}"
        if not ws.exists():
            return None
        cmd = [sys.executable, "-I", str(ROOT / "scripts" / "run_g_scoring.py"),
               str(ws), "--label", f"p0{arm}_t{tid}", "--no-mutation"]
        if corpus != "corpus-003":
            cmd += ["--corpus", corpus]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900,
                           env={"PYTHONIOENCODING": "utf-8", "PATH": "", "SYSTEMROOT":
                                __import__("os").environ.get("SYSTEMROOT", "")})
        if r.returncode != 0 or not f.exists():
            return {"error": (r.stdout or "")[-200:] + (r.stderr or "")[-200:]}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def unfounded_counts(conn, tid: int, common_req: str | None = None) -> tuple[int, int]:
    """(失败条数, 无依据失败条数)。common_req=None 用任务自身需求（C7 函数原样）；
    给文本则两臂用同一把尺（泛需求）。"""
    from test_agent.skills import expectation_audit as ea
    rows = conn.execute("SELECT nodeid FROM test_case_results WHERE task_id=? "
                        "AND status='failed'", (tid,)).fetchall()
    nodeids = [r[0] for r in rows]
    if common_req is None:
        a = ea.audit_retained_failures(conn, tid, "", failing_nodeids=lambda s: nodeids)
        return a["n_failed"], a["n_unfounded"]
    idx = ea._case_index(conn, tid)
    n_unf = 0
    for nid in nodeids:
        name = (nid or "").split("::")[-1].split("/")[0]
        basis, expected = idx.get(ea.normalize_case_name(name), ("", ""))
        if ea.is_unfounded(basis, expected, common_req):
            n_unf += 1
    return len(nodeids), n_unf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-idx", type=int, default=5)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    conn = _conn()
    common_req = vague_requirement()
    per: dict[str, dict[str, list]] = {k: {"a": [], "b": []} for k in PROJECTS}
    for arm in ("a", "b"):
        for f in sorted(CELLS.glob(f"scale_p0{arm}_*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            if d["idx"] > a.max_idx or d["key"] not in PROJECTS:
                continue
            role, corpus = PROJECTS[d["key"]]
            tid = d["task_id"]
            tok = conn.execute("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t "
                               "FROM llm_calls WHERE task_id=?", (tid,)).fetchone()["t"]
            n_cases = conn.execute("SELECT COUNT(*) c FROM test_case_results WHERE task_id=?",
                                   (tid,)).fetchone()["c"]
            fails = conn.execute("SELECT nodeid, message FROM test_case_results WHERE task_id=? "
                                 "AND status='failed'", (tid,)).fetchall()
            rec = {"idx": d["idx"], "tid": tid, "status": d["status"], "outline": d.get("outline"),
                   "cost": d.get("cost_yuan_est"), "tokens": tok, "n_cases": n_cases,
                   "n_failed": len(fails), "detected": None, "score": None}
            if d["key"] == "js-calc":
                rec["detected"] = any("clamp" in (r["nodeid"] or "").lower() for r in fails)
            elif d["key"] == "go-api-book":
                rec["detected"] = any("delete" in (r["message"] or "").lower()
                                      and "want 404" in (r["message"] or "").lower() for r in fails)
            elif corpus:
                if a.score:
                    rec["score"] = ensure_score(arm, corpus, tid)
                else:
                    sf = score_file(arm, corpus, tid)
                    if sf.exists():
                        rec["score"] = json.loads(sf.read_text(encoding="utf-8"))
                rate = (rec.get("score") or {}).get("detection_rate")
                rec["detected"] = (rate is not None and rate >= 0.2) if rate is not None else None
            if d["status"] == "failed" and fails:
                rec["unf_own"] = unfounded_counts(conn, tid)[1]
                rec["unf_common"] = unfounded_counts(conn, tid, common_req)[1]
            else:
                rec["unf_own"] = rec["unf_common"] = 0
            per[d["key"]][arm].append(rec)

    def agg(key, arm):
        rs = per[key][arm]
        n = len(rs)
        det = [r for r in rs if r["detected"] is not None]
        fp = [r for r in rs if r["status"] == "failed"]
        return {
            "n": n, "n_scored": len(det), "detected": sum(1 for r in det if r["detected"]),
            "rate": (round(sum(1 for r in det if r["detected"]) / len(det), 3) if det else None),
            "fp_cells": len(fp), "fp_rate": (round(len(fp) / n, 3) if n else None),
            "cases_per_cell": round(sum(r["n_cases"] for r in rs) / n, 1) if n else None,
            "tokens_per_cell": round(sum(r["tokens"] for r in rs) / n) if n else None,
            "cost_per_cell": round(sum(r["cost"] or 0 for r in rs) / n, 4) if n else None,
            "failed_total": sum(r["n_failed"] for r in rs),
            "unf_own_total": sum(r["unf_own"] for r in rs),
            "unf_common_total": sum(r["unf_common"] for r in rs),
        }

    table = {k: {"a": agg(k, "a"), "b": agg(k, "b")} for k in PROJECTS}
    # 余量分类（卡 §3）：按 A 臂格级检出率
    headroom: dict[str, str] = {}
    pooled = {"a": [], "b": []}
    for k, (role, _c) in PROJECTS.items():
        if role != "defect":
            continue
        ra = table[k]["a"]["rate"]
        if ra is None:
            headroom[k] = "未评分"
        elif ra >= HEADROOM_NONE:
            headroom[k] = "无余量"
        else:
            headroom[k] = "充足" if ra <= HEADROOM_FULL else "有限"
            if table[k]["b"]["n_scored"]:
                pooled["a"] += [1 if r["detected"] else 0 for r in per[k]["a"] if r["detected"] is not None]
                pooled["b"] += [1 if r["detected"] else 0 for r in per[k]["b"] if r["detected"] is not None]
    h1 = None
    if pooled["a"] and pooled["b"]:
        pa = sum(pooled["a"]) / len(pooled["a"])
        pb = sum(pooled["b"]) / len(pooled["b"])
        h1 = {"n_a": len(pooled["a"]), "n_b": len(pooled["b"]), "rate_a": round(pa, 3),
              "rate_b": round(pb, 3), "diff_pp": round((pb - pa) * 100, 1),
              "ci_a": wilson(sum(pooled["a"]), len(pooled["a"])),
              "ci_b": wilson(sum(pooled["b"]), len(pooled["b"])),
              "diff_ci95": boot_diff_ci(pooled["a"], pooled["b"]),
              "pass": (pb - pa) >= H1_PP}
    fp_a, fp_b = table["coupon-api"]["a"], table["coupon-api"]["b"]
    def _unf_pass(x, y):
        sa = (x["unf_own_total"] / x["failed_total"]) if x["failed_total"] else 0.0
        sb = (y["unf_own_total"] / y["failed_total"]) if y["failed_total"] else 0.0
        return sb <= (sa * 2 if sa > 0 else 0.10), round(sa, 3), round(sb, 3)
    h2_fp = (fp_b["fp_rate"] or 0) - (fp_a["fp_rate"] or 0) if (fp_a["n"] and fp_b["n"]) else None
    h2_unf_ok, h2_unf_a, h2_unf_b = _unf_pass(fp_a, fp_b)
    h2 = {"fp_rate_a": fp_a["fp_rate"], "fp_rate_b": fp_b["fp_rate"],
          "fp_diff_pp": round(h2_fp * 100, 1) if h2_fp is not None else None,
          "unf_share_own_a": h2_unf_a, "unf_share_own_b": h2_unf_b,
          "pass": (h2_fp is not None and h2_fp <= H2_FP_PP and h2_unf_ok)}
    verdict = ("无法检验（没有有余量的项目）" if h1 is None else
               ("H1 过" if h1["pass"] else "H1 不过"))
    if h1 is not None and h1["pass"]:
        verdict += "；H2 过，进阶段 1" if h2["pass"] else "；H2 不过，大纲须带预期依据约束"
    out = {"max_idx": a.max_idx, "headroom": headroom, "H1": h1, "H2": h2,
           "H3": {k: {"tokens_b_over_a": (round(table[k]["b"]["tokens_per_cell"] /
                                               table[k]["a"]["tokens_per_cell"], 2)
                                        if table[k]["a"]["tokens_per_cell"] and table[k]["b"]["tokens_per_cell"] else None),
                      "cases_b_over_a": (round(table[k]["b"]["cases_per_cell"] /
                                               table[k]["a"]["cases_per_cell"], 2)
                                         if table[k]["a"]["cases_per_cell"] and table[k]["b"]["cases_per_cell"] else None)}
                  for k in PROJECTS},
           "table": table, "per_cell": per, "verdict": verdict,
           "cost_total_chat_price": round(sum(r["cost"] or 0 for k in per for arm in ("a", "b")
                                              for r in per[k][arm]), 4)}

    print(f"== 阶段 0 判读（idx ≤ {a.max_idx}；口径=预注册卡）==")
    for k in PROJECTS:
        for arm, lab in (("a", "A 泛需求"), ("b", "B +大纲")):
            t = table[k][arm]
            det = (f"检出 {t['detected']}/{t['n_scored']}={t['rate']}" if t["n_scored"]
                   else "检出 未评分")
            print(f"  {k:13s} {lab:8s} n={t['n']} {det:22s} 误报格 {t['fp_cells']} "
                  f"用例/格 {t['cases_per_cell']} token/格 {t['tokens_per_cell']} "
                  f"¥/格 {t['cost_per_cell']} 失败 {t['failed_total']} "
                  f"无据(自身需求) {t['unf_own_total']} 无据(同一把尺) {t['unf_common_total']}")
    print("  余量分类：", json.dumps(headroom, ensure_ascii=False))
    print("  H1：", json.dumps(h1, ensure_ascii=False))
    print("  H2：", json.dumps(h2, ensure_ascii=False))
    print("  H3（B/A 倍数）：", json.dumps(out["H3"], ensure_ascii=False))
    print("  花费（chat 价估算）：", out["cost_total_chat_price"])
    print("  判读：", verdict, "（每臂不足 30 格一律为方向性读数）")
    dest = ROOT / "data" / "reports" / "agentloop_scale" / f"phase0_judgment_{a.max_idx}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[written]", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
