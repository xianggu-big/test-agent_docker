# -*- coding: utf-8 -*-
"""agentloop_kpi.py——Agent Loop 两节点的任务契约指标（任务四 AL-6，设计报告 §1）。

全部确定性计算：运行账本（agent_runs/agent_steps）+ 工作区源码 + 生成用例，不靠模型自评。

strategy 指标：S-K1 收尾率 / S-K2 具体率 / S-K3 真实率 / S-K4 落地率 / S-K5 平均步数
diagnose 指标：D-K1 类别准确率 / D-K2 引用有效率 / D-K3 置信校准 / D-K4 建议自洽率 /
              D-K5 恢复率（观察项）

用法：
  python -I scripts/agentloop_kpi.py --since "2026-11-26 20:00"   # 点火后的格子
  python -I scripts/agentloop_kpi.py --baseline                    # v1 测量批（盲跑基线）
范围：--since 时刻之后的 agent_runs；--baseline = prompt_version='v1' 的 30 格。
具体/泛指判定复用 tool_loop._strategy_is_generic 的抽词口径（_strategy_extract_ids）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "test_agent.db"
# 语言关键字（设计 §1.1：去掉语言关键字）——抽词口径与 tool_loop 一致
KEYWORDS = {"def", "class", "return", "import", "from", "if", "else", "elif",
            "while", "for", "in", "not", "and", "or", "None", "True", "False",
            "const", "let", "var", "function", "await", "async", "export",
            "import", "require", "expect", "describe", "it", "test"}
GENERIC_BANNED = {"tests", "test", "src", "lib", "app", "unit", "case", "cases",
                  "testing", "coverage", "function", "code", "file", "files"}
IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{2,}")
IDENT_STOP = {"the", "and", "for", "with", "this", "that", "should", "then",
              "when", "where", "each", "case", "test", "tests", "testing",
              "expect", "assert", "input", "output", "return", "value",
              "check", "cover", "coverage", "missing", "gap", "lack", "need",
              "add", "run", "unit", "error", "fail", "failed", "none", "null",
              "true", "false", "example", "increase", "improve", "enhance",
              "ensure", "verify"}


def extract_ids(text: str) -> set[str]:
    """从文本抽标识符（设计 §1.1 口径：字母开头 ≥3 去关键字；路径形直接算）。
    连写形式（add/div/clamp、src/calc.js）拆段入集——真实率判定按段匹配源码。"""
    out: set[str] = set()
    for m in IDENT_RE.findall(text or ""):
        token = m.lower().strip("./-_")
        if not token or token in GENERIC_BANNED or token in IDENT_STOP:
            continue
        out.add(token)
        if "/" in token or "." in token or "_" in token:
            for part in re.split(r"[./_]", token):
                if len(part) >= 3 and part not in GENERIC_BANNED and part not in IDENT_STOP:
                    out.add(part)
    return out


def strategy_priorities(decision_json: str) -> list[dict]:
    try:
        d = json.loads(decision_json or "{}")
    except Exception:       # noqa: BLE001
        return []
    return ((d.get("strategy") or {}).get("priorities")) or []


def _conn():
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_runs(conn, since: str | None, baseline: bool) -> list[dict]:
    q = ("SELECT r.id, r.task_id, r.status, r.prompt_version, r.started_ms, r.ended_ms, "
         "t.error FROM agent_runs r LEFT JOIN test_tasks t ON t.id=r.task_id WHERE 1=1")
    args: list = []
    if baseline:
        q += " AND r.prompt_version='v1'"
    elif since:
        # 点火时刻（本地时间）之后启动的 run——SQLite strftime 会把字符串当 UTC，
        # 本地时区换算必须在 Python 侧做（实测踩坑：UTC 语义把窗口推到未来=0 run）
        import datetime as _dt
        epoch_ms = int(_dt.datetime.strptime(since, "%Y-%m-%d %H:%M")
                       .timestamp() * 1000)
        q += " AND r.started_ms >= ?"
        args.append(epoch_ms)
    q += " ORDER BY r.id"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def task_steps(conn, task_id: int, mode: str) -> list[dict]:
    """按 task 取某模式的全部循环步（跨 run 行——两阶段任务有 prep/exec 两条 run，
    诊断步挂在阶段二 run 下，按单 run 查会漏）。"""
    rows = conn.execute(
        "SELECT s.step_no, s.action_type, s.decision_json FROM agent_steps s "
        "JOIN agent_runs r ON r.id=s.run_id WHERE r.task_id=? AND s.mode=? "
        "ORDER BY s.run_id, s.step_no", (task_id, mode)).fetchall()
    return [dict(r) for r in rows]


def strategy_metrics(conn, runs: list[dict], workspace_of) -> dict:
    """S-K1..K5。工作区符号集从 workspace_of(task_id) 取（源码+用例文件内容）。"""
    n_loops = 0
    n_finished = 0
    n_pri = 0
    n_concrete = 0
    n_concrete_real = 0
    n_real_landed = 0
    steps_sum = 0
    samples: list[dict] = []
    for r in runs:
        steps = task_steps(conn, r["task_id"], "strategy")
        if not steps:
            continue          # 非 strategy 循环（诊断行等）不计
        n_loops += 1
        loop_steps = len(steps)
        steps_sum += loop_steps
        finishes = [s for s in steps if s["action_type"] == "finish"]
        if finishes:
            n_finished += 1
        syms = workspace_of(conn, r["task_id"])
        landed = workspace_of(conn, r["task_id"], cases_only=True)
        for f in finishes[-1:]:
            pri = strategy_priorities(f["decision_json"])
            for p in pri:
                n_pri += 1
                ah = p.get("args_hint") or {}
                blob = " ".join([str(p.get("why") or ""),
                                 str(ah.get("feedback") or "") if isinstance(ah, dict) else "",
                                 " ".join(ah.get("targets") or []) if isinstance(ah, dict) else ""])
                ids = extract_ids(blob)
                concrete = bool(ids)
                if not concrete:
                    continue
                n_concrete += 1
                real = {i for i in ids if any(i in s for s in syms)}
                if real:
                    n_concrete_real += 1
                    if any(any(i in c for c in landed) for i in real):
                        n_real_landed += 1
                if len(samples) < 60:
                    samples.append({"task_id": r["task_id"], "why": str(p.get("why"))[:120],
                                    "ids": sorted(ids)[:6], "concrete": True,
                                    "real": bool(real)})
    return {
        "n_loops": n_loops,
        "S_K1_finish_rate": (n_finished / n_loops) if n_loops else None,
        "n_priorities": n_pri,
        "S_K2_concrete_rate": (n_concrete / n_pri) if n_pri else None,
        "S_K3_real_rate": (n_concrete_real / n_concrete) if n_concrete else None,
        "S_K4_landed_rate": (n_real_landed / n_concrete) if n_concrete else None,
        "S_K5_avg_steps": (steps_sum / n_loops) if n_loops else None,
        "samples": samples,
    }


def workspace_of(conn, task_id: int, cases_only: bool = False) -> list[str]:
    """工作区源码符号集（粗粒度：文件内容字符串列表；cases_only=True 只取用例）。
    真实率=符号在源码里出现；落地率=符号出现在生成用例里。"""
    ws = ROOT / "data" / "workspaces" / f"task_{task_id}"
    texts: list[str] = []
    if ws.exists():
        for pat in ("test_gen_r*.js", "test_gen_r*.py", "test_gen_fb*.py", "test_gen*.ts"):
            for p in ws.glob(pat):
                texts.append(p.read_text(encoding="utf-8", errors="ignore"))
        if not cases_only:
            for p in ws.rglob("*.js"):
                if "node_modules" not in str(p) and "test" not in p.name:
                    texts.append(p.read_text(encoding="utf-8", errors="ignore"))
            for p in ws.rglob("*.py"):
                if "test" not in p.name and "node_modules" not in str(p):
                    texts.append(p.read_text(encoding="utf-8", errors="ignore"))
    return texts


def diagnose_metrics(conn, runs: list[dict], labels: dict) -> dict:
    """D-K1..K4（D-K5 只观察）。labels={task_id: {"class": 设定类别}}——夹具标签。
    两阶段任务有 prep/exec 两条 run 行——按 task 去重（每格只判一次）。"""
    n_valid = 0
    n_kind_ok = 0
    n_refs = 0
    n_refs_valid = 0
    n_conf_ok = 0
    n_conf_cases = 0
    n_suggest_ok = 0
    n_suggest = 0
    per_cell: list[dict] = []
    seen_tasks: set[int] = set()
    consistent = {"scaffold_error": {"scaffold_fix", "generate"},
                  "expectation_dispute": {"generate", "report_human"},
                  "env_error": {"env_fix"},
                  "product_defect": {"report_human"}}
    for r in runs:
        if r["task_id"] in seen_tasks:
            continue
        seen_tasks.add(r["task_id"])
        lab = labels.get(str(r["task_id"])) or labels.get(r["task_id"])
        if not lab:
            continue
        steps = task_steps(conn, r["task_id"], "diagnose")
        if not steps:
            continue          # 夹具未触发诊断（不算数也不算失败）
        n_valid += 1
        fin = [s for s in steps if s["action_type"] == "finish"]
        if not fin:
            per_cell.append({"task_id": r["task_id"], "valid": True, "finished": False})
            continue
        try:
            d = json.loads(fin[-1]["decision_json"] or "{}").get("diagnosis") or {}
        except Exception:   # noqa: BLE001
            d = {}
        kind_ok = d.get("kind") == lab.get("class")
        n_kind_ok += bool(kind_ok)
        refs = d.get("evidence_refs") or []
        max_step = len(steps)
        ok_refs = [x for x in refs if isinstance(x, int) and 0 <= x <= max_step]
        n_refs += len(refs)
        n_refs_valid += len(ok_refs)
        conf = d.get("confidence")
        expect_conf = lab.get("confidence")          # 夹具设定：充分=medium/high，不足=low
        if expect_conf in ("medium", "high"):
            n_conf_cases += 1
            n_conf_ok += conf in ("medium", "high")
        suggest_ok = d.get("suggested_action") in consistent.get(d.get("kind"), set())
        n_suggest += 1
        n_suggest_ok += bool(suggest_ok)
        per_cell.append({"task_id": r["task_id"], "valid": True, "finished": True,
                         "kind": d.get("kind"), "expected": lab.get("class"),
                         "kind_ok": kind_ok, "confidence": conf,
                         "refs": refs, "refs_ok": bool(len(refs) == len(ok_refs)),
                         "suggest_ok": suggest_ok})
    return {
        "n_valid_cells": n_valid,
        "D_K1_kind_accuracy": f"{n_kind_ok}/{n_valid}" if n_valid else None,
        "D_K2_ref_valid_rate": (n_refs_valid / n_refs) if n_refs else None,
        "D_K3_confidence_calibrated": f"{n_conf_ok}/{n_conf_cases}" if n_conf_cases else None,
        "D_K4_suggestion_consistent": f"{n_suggest_ok}/{n_suggest}" if n_suggest else None,
        "per_cell": per_cell,
    }


def cost_metrics(conn, runs: list[dict]) -> dict:
    durs = []
    for r in runs:
        if r.get("started_ms") and r.get("ended_ms"):
            durs.append((r["ended_ms"] - r["started_ms"]) / 1000)
    durs.sort()
    p50 = durs[len(durs) // 2] if durs else None
    tids = [r["task_id"] for r in runs]
    p = c = 0
    for t in tids:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c "
            "FROM llm_calls WHERE task_id=?", (t,)).fetchone()
        p += row["p"]
        c += row["c"]
    return {"n_runs": len(runs), "P50_s": round(p50, 1) if p50 else None,
            "llm_prompt_tokens": p, "llm_completion_tokens": c}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="只统计该本地时刻之后启动的 run")
    ap.add_argument("--baseline", action="store_true", help="v1 测量批（盲跑基线）")
    ap.add_argument("--labels", default=str(ROOT / "data/reports/agentloop_scale/fixture_labels.json"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    conn = _conn()
    runs = load_runs(conn, args.since, args.baseline)
    try:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) \
            if Path(args.labels).exists() else {}
        rep = {"range": "baseline(v1)" if args.baseline else f"since {args.since}",
               "runs": len(runs),
               "strategy": strategy_metrics(conn, runs, workspace_of),
               "diagnose": diagnose_metrics(conn, runs, labels),
               "cost": cost_metrics(conn, runs)}
    finally:
        conn.close()
    out = json.dumps(rep, ensure_ascii=False, indent=1)
    print(out)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
