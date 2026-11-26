# -*- coding: utf-8 -*-
"""agentloop_judge_v3.py——规模批（v3）六组预注册判据的正式判定脚本（A2/A6，2026-11-27）。

判据真源：docs/AgentLoop-测试任务报告-20261126.md §6（判据不许现场改）。
分母口径：规模批 45 格 = data/reports/agentloop_scale/cells/scale_v3_*.json 的
task_id 集合（2634 + 2649..2692）；诊断判据用夹具探针格（fixture_labels.json）。
A0 修订说明：判定在 A0-1/A0-2/A0-3 三处平台 bug 修复后重跑（解折层修复不改变
批内行为数据，只改变指标层对既有事实的读法）。

两版并报（A2 第 2/3 条）：
  - "具体"口径：宽松版=任意 ≥3 字母非停用词；加严版（口径修订版，判定用）=
    带 _ . ( ) 的符号，或源码里真实存在的标识符。
  - S-K3：子串匹配（偏宽）与词边界匹配两版。
输出：stdout 判定表（供报告逐字引用）+ JSON（data/reports/agentloop_scale/judgment_v3.json）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CELLS = ROOT / "data" / "reports" / "agentloop_scale" / "cells"
LABELS = ROOT / "data" / "reports" / "agentloop_scale" / "fixture_labels.json"
PROD_DB = ROOT / "data" / "test_agent.db"

# v1 盲跑基线（预注册对照值，来自指标表 v1 与任务卡 A1 已知事实）
V1 = {"P50_s": 36.2, "token_per_cell": 19519, "completion_strict": 1.0,
      "hallucination": 0.0476, "tool_micro": 0.864}


def _conn():
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_scale_tasks() -> dict[str, dict]:
    out = {}
    for p in sorted(CELLS.glob("scale_v3_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[p.name] = d
    return out


# ---------- 检出判定（A2） ----------

def detection_per_cell(conn, tasks: dict[str, dict]) -> dict:
    """A2 三项目检出语义（预注册口径）：
    js-calc：≥1 条 clamp 失败用例=检出（植入缺陷=clamp hi-1）；
    go-api-book：≥1 条 DELETE→want 404 失败用例=DELETE 检出（植入缺陷）；
      只靠 POST title 常识预期判"疑似缺陷"的不算 DELETE 检出；
    coupon-api：零失败用例=零误报通过。"""
    per = {}
    for name, d in tasks.items():
        tid = d["task_id"]
        rows = conn.execute(
            "SELECT nodeid, message FROM test_case_results WHERE task_id=? "
            "AND status='failed'", (tid,)).fetchall()
        fails = [dict(r) for r in rows]
        proj = d["key"]
        if proj == "js-calc":
            hit = [f for f in fails if "clamp" in (f["nodeid"] or "").lower()]
        elif proj == "go-api-book":
            # 大小写不敏感（2675 实锤：模型输出小写 "delete ... want 404"，语义同检出）
            hit = [f for f in fails
                   if "delete" in (f["message"] or "").lower()
                   and "want 404" in (f["message"] or "").lower()]
        else:  # coupon-api：零误报
            hit = []
        per[name] = {"task_id": tid, "project": proj, "n_failed": len(fails),
                     "n_semantic_hit": len(hit),
                     "detected": bool(hit) if proj != "coupon-api" else (len(fails) == 0),
                     "fail_names": [f["nodeid"].split("::")[-1] for f in fails][:6]}
    agg = {"js-calc": [0, 0], "go-api-book": [0, 0], "coupon-api": [0, 0]}
    for name, p in per.items():
        agg[p["project"]][1] += 1
        if p["detected"]:
            agg[p["project"]][0] += 1
    return {"per_cell": per, "aggregate": {k: f"{v[0]}/{v[1]}" for k, v in agg.items()},
            "aggregate_raw": agg}


# ---------- "具体/真实"判定（A3，两版口径） ----------

def strict_ids(ids: set[str], syms: list[str]) -> set[str]:
    """加严版（口径修订版）：带 _ . 的符号，或源码里真实存在的标识符。"""
    out = set()
    for i in ids:
        if "_" in i or "." in i or any(i in s for s in syms):
            out.add(i)
    return out


def word_boundary_real(ids: set[str], syms: list[str]) -> set[str]:
    """S-K3 词边界版：re \\b 匹配，防子串误中（如 date 中子串 at）。"""
    out = set()
    for i in ids:
        if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(i)}(?![A-Za-z0-9_])", s)
               for s in syms):
            out.add(i)
    return out


def strategy_two_versions(conn, task_ids: list[int]) -> dict:
    """S-K1..K5 限定规模批 45 格；S-K2/S-K3 宽松+加严并报；S-K3 词边界版。"""
    from scripts.agentloop_kpi import (strategy_priorities, extract_ids,  # noqa: SC
                                       workspace_of, task_steps)
    runs = []
    for tid in task_ids:
        r = conn.execute(
            "SELECT r.task_id AS task_id FROM agent_runs r WHERE r.task_id=? "
            "ORDER BY r.id DESC LIMIT 1", (tid,)).fetchone()
        if r is not None:
            runs.append(dict(r))
    n_loops = n_finished = 0
    n_pri = n_conc_loose = n_real_loose = n_land_loose = 0
    n_conc_strict = n_real_strict = n_land_strict = 0
    n_real_wb = 0
    missing_finish = []
    for r in runs:
        steps = task_steps(conn, r["task_id"], "strategy")
        if not steps:
            continue
        n_loops += 1
        finishes = [s for s in steps if s["action_type"] == "finish"]
        if finishes:
            n_finished += 1
        else:
            missing_finish.append(r["task_id"])
        syms = workspace_of(conn, r["task_id"])
        landed = workspace_of(conn, r["task_id"], cases_only=True)
        for f in finishes[-1:]:
            for p in strategy_priorities(f["decision_json"]):
                n_pri += 1
                ah = p.get("args_hint") or {}
                blob = " ".join([str(p.get("why") or ""),
                                 str(ah.get("feedback") or "") if isinstance(ah, dict) else "",
                                 " ".join(ah.get("targets") or []) if isinstance(ah, dict) else ""])
                ids = extract_ids(blob)
                if not ids:
                    continue
                n_conc_loose += 1
                sids = strict_ids(ids, syms)
                real_loose = {i for i in ids if any(i in s for s in syms)}
                real_wb = word_boundary_real(ids, syms)
                if sids:
                    n_conc_strict += 1
                    real_strict = {i for i in sids if any(i in s for s in syms)}
                    if real_strict:
                        n_real_strict += 1
                        if any(any(i in c for c in landed) for i in real_strict):
                            n_land_strict += 1
                if real_loose:
                    n_real_loose += 1
                    if any(any(i in c for c in landed) for i in real_loose):
                        n_land_loose += 1
                if real_wb:
                    n_real_wb += 1
    return {
        "n_loops": n_loops,
        "S_K1": f"{n_finished}/{n_loops}", "S_K1_rate": n_finished / n_loops,
        "missing_finish_tasks": missing_finish,
        "n_priorities": n_pri,
        "S_K2_loose": n_conc_loose / n_pri if n_pri else None,
        "S_K2_strict": n_conc_strict / n_pri if n_pri else None,
        "S_K3_loose_substring": n_real_loose / n_conc_loose if n_conc_loose else None,
        "S_K3_strict": n_real_strict / n_conc_strict if n_conc_strict else None,
        "S_K3_word_boundary": n_real_wb / n_conc_loose if n_conc_loose else None,
        "S_K4_loose": n_land_loose / n_conc_loose if n_conc_loose else None,
        "S_K4_strict": n_land_strict / n_conc_strict if n_conc_strict else None,
    }


# ---------- 主流程 ----------

def main() -> None:
    conn = _conn()
    tasks = load_scale_tasks()
    task_ids = sorted(d["task_id"] for d in tasks.values())
    out: dict = {"n_cells": len(task_ids), "task_range": [task_ids[0], task_ids[-1]]}

    # A1 安全
    driver_error = [n for n, d in tasks.items()
                    if "driver_error" in json.dumps(d.get("error") or "", ensure_ascii=False)]
    terminal = [n for n, d in tasks.items() if d.get("status") in ("done", "failed")]
    four_ok = [n for n, d in tasks.items()
               if (d.get("four_table") or {}).get("ok") is True]
    durations = sorted(d["duration_s"] for d in tasks.values() if d.get("duration_s"))
    wall_p50 = durations[len(durations) // 2] if durations else None
    # P50 口径与 v1 对齐（kpi cost_metrics：run 行 ended-started 毫秒中位数）；
    # 结算件 duration_s 是含排队的墙钟，只作旁注（wall_p50）
    run_durs = []
    for tid in task_ids:
        rr = conn.execute("SELECT started_ms, ended_ms FROM agent_runs WHERE task_id=?",
                          (tid,)).fetchall()
        for r in rr:
            if r["started_ms"] and r["ended_ms"]:
                run_durs.append((r["ended_ms"] - r["started_ms"]) / 1000)
    run_durs.sort()
    p50 = run_durs[len(run_durs) // 2] if run_durs else None
    out["A1"] = {
        "driver_error_cells": len(driver_error),
        "terminal_cells": f"{len(terminal)}/45",
        "four_table_ok": f"{len(four_ok)}/45",
        "strict_completion": len(terminal) / 45,
        "threshold": "完成率 >= v1(100%) - 5pp = 95%；driver_error=0",
        "P50_s_scale_batch": round(p50, 1) if p50 else None,
        "P50_wall_clock_settlement": wall_p50,
    }

    # A2 检出
    det = detection_per_cell(conn, tasks)
    out["A2"] = {
        "js_clamp": det["aggregate"]["js-calc"], "js_threshold": "≥13/15",
        "go_delete": det["aggregate"]["go-api-book"], "go_threshold": "≥13/15",
        "coupon_zero_fp": det["aggregate"]["coupon-api"], "coupon_threshold": "≥14/15",
        "per_cell": det["per_cell"],
    }

    # A3 策略（两版口径）
    st = strategy_two_versions(conn, task_ids)
    out["A3"] = {k: v for k, v in st.items()}

    # A4 诊断（夹具探针；两版：全 12 格 / 剔除协议修订前 2637/2638）
    from scripts.agentloop_kpi import diagnose_metrics
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    probe_ids = sorted(int(t) for t in labels)
    runs_probe = [{"task_id": t} for t in probe_ids]
    dm_all = diagnose_metrics(conn, runs_probe, labels)
    labels_trim = {k: v for k, v in labels.items() if k not in ("2637", "2638")}
    dm_trim = diagnose_metrics(conn, [{"task_id": int(t)} for t in labels_trim],
                               labels_trim)
    out["A4"] = {"all_probes": {k: v for k, v in dm_all.items()
                                if not isinstance(v, list)},
                 "excl_2637_2638": {k: v for k, v in dm_trim.items()
                                    if not isinstance(v, list)},
                 "thresholds": {"D_K1": "≥8/9", "D_K2": "100%", "D_K3": "≥2/3",
                                "D_K4": "100%"}}

    # A5 诚实与工具（45 格 run 行）
    from scripts.agentloop_kpi import load_runs
    run_rows = []
    for tid in task_ids:
        rr = conn.execute("SELECT id FROM agent_runs WHERE task_id=? ORDER BY id",
                          (tid,)).fetchall()
        run_rows.extend({"id": r["id"]} for r in rr)
    from test_agent.skills import run_metrics
    c1t = c1i = c2t = c2i = 0
    call_ok = call_all = denied = 0
    for rr in run_rows:
        h = run_metrics.hallucination_checks_for_run(conn, rr["id"])
        c1t += h["c1"]["total"]; c1i += h["c1"]["invalid"]
        c2t += h["c2"]["total"]; c2i += h["c2"]["invalid"]
        tm = run_metrics.tool_metrics_for_run(conn, rr["id"])
        call_all += tm["calls"]; call_ok += tm["counts"]["ok"]
        denied += tm["counts"]["denied_policy"] + tm["counts"]["denied_registry"]
    hall_rate = (c1i + c2i) / (c1t + c2t) if (c1t + c2t) else None
    out["A5"] = {
        "checkable_hallucination_rate": hall_rate, "threshold": "≤5%",
        "c1": f"{c1i}/{c1t}", "c2": f"{c2i}/{c2t}",
        "tool_micro_call_success": call_ok / call_all if call_all else None,
        "tool_calls": f"{call_ok}/{call_all}", "threshold_tool": "≥90%",
        "rejection_rate": denied / call_all if call_all else None,
        "threshold_rej": "≤10%",
    }

    # A6 成本（45 格 token 与 P50 对照 v1）
    tot_tok = 0
    n_run = 0
    for tid in task_ids:
        rr = conn.execute(
            "SELECT prompt_tokens, completion_tokens FROM llm_calls WHERE task_id=?",
            (tid,)).fetchall()
        for r in rr:
            tot_tok += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
        n_run += 1
    tok_per_cell = tot_tok / n_run if n_run else None
    out["A6"] = {
        "token_per_cell_v3": round(tok_per_cell, 0), "token_per_cell_v1": V1["token_per_cell"],
        "token_growth": tok_per_cell / V1["token_per_cell"] - 1,
        "threshold_token": "≤+60%",
        "P50_v3": p50, "P50_v1": V1["P50_s"],
        "P50_growth_s": p50 - V1["P50_s"], "threshold_P50": "≤+90 秒",
    }

    dest = ROOT / "data" / "reports" / "agentloop_scale" / "judgment_v3.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    print(json.dumps({k: (v if k != "A2" else {kk: vv for kk, vv in v.items()
                                               if kk != "per_cell"})
                      for k, v in out.items()}, ensure_ascii=False, indent=1,
                     default=str))
    print(f"\n[written] {dest}")


if __name__ == "__main__":
    main()
