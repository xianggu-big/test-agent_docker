# -*- coding: utf-8 -*-
"""主方向指标表 v0 一键重现（学院达标 T8，2026-11-25）——零 LLM。

三个数据源分账不混算（设计稿 §11）：
  A. 48 格 Agent Loop 留档 data/reports/agent_loop_2x2/cell_*.json
     → 完成率（按指标契约 §1.1 outcome 判定）+ 响应时间（duration_s）
  B. 49 个归档临时库 data/reports/agent_loop_2x2/tmpdb/*.db
     → Agent 工具调用成功率（skill_invocations JOIN skills，name LIKE 'test.%'）
       + 循环步数 + agent LLM 行数
  C. 生产库 data/test_agent.db（只读）
     → 响应时间近似值（agent_sessions started→ended，秒级，approx 标）+ 缺失率
       + 任务状态计数（不做全库完成率——E9 失败语料实验混算无意义，设计稿 §11）
  D. bench3/Wave2 准确率：直接引用报告（不重算——引用 docs/bench3三方基准实验报告）。

产出：stdout 报告 + data/reports/metrics_v0/metrics_v0.json（数字与指标表 v0 逐项对应）。
验收：docs/主方向指标表-v0-20261124.md 里每个数字跑本脚本可重现。
"""
from __future__ import annotations

import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CELL_GLOB = str(ROOT / "data" / "reports" / "agent_loop_2x2" / "cell_*.json")
TMPDB_GLOB = str(ROOT / "data" / "reports" / "agent_loop_2x2" / "tmpdb" / "*" / "*.db")
PROD_DB = ROOT / "data" / "test_agent.db"
OUT_DIR = ROOT / "data" / "reports" / "metrics_v0"


def source_a_cells() -> dict:
    """A 源：48 格留档 → 契约 §1.1 outcome 判定 + duration 汇总。"""
    from test_agent.skills.run_metrics import classify_outcome
    files = sorted(glob.glob(CELL_GLOB))
    outcomes: dict[str, int] = {}
    durations: list[float] = []
    n_driver_error = 0
    stop_kinds: dict[str, int] = {}
    for f in files:
        s = json.load(open(f, encoding="utf-8"))
        if s.get("driver_error"):
            n_driver_error += 1
        # 有效执行证据：funnel 最后轮的 passed/failed（检出格 failed>0=真执行证人）
        rounds = (s.get("funnel") or {}).get("rounds") or []
        last = rounds[-1] if rounds else {}
        passed_n = int(last.get("passed") or 0)
        failed_n = int(last.get("failed") or 0)
        oc = classify_outcome(
            run_status="done" if s.get("status") == "done" else
            ("cancelled" if s.get("status") == "cancelled" else "failed"),
            task_status=s.get("status"),
            failure_kind=_kind_of(s),
            verdict=s.get("verdict"),
            passed=passed_n,
            failed=failed_n,
            has_report=s.get("status") in ("done", "failed"),
            stop_reason=s.get("stop_reason") or "")
        outcomes[oc] = outcomes.get(oc, 0) + 1
        if s.get("duration_s"):
            durations.append(float(s["duration_s"]))
        kind = _kind_of(s) or "unknown"
        stop_kinds[kind] = stop_kinds.get(kind, 0) + 1
    D = sum(v for k, v in outcomes.items() if k != "not_completed_user")
    strict = (outcomes.get("completed", 0) / D) if D else None
    return {"n_cells": len(files), "n_driver_error": n_driver_error,
            "outcomes": outcomes, "stop_kinds": stop_kinds,
            "denominator_D": D, "strict_completion": strict,
            "duration_s": _stats(durations)}


def _kind_of(s: dict) -> str | None:
    """cell json 没有 failure_kind 字段——按 stop_reason 前缀推断（契约 §1.1 锚点）。"""
    vr = s.get("verdict")
    if vr == "pass":
        return "none"
    sr = s.get("stop_reason") or ""
    if sr.startswith("疑似产品缺陷") or sr.startswith("补测完成") or sr.startswith("冻结证人"):
        return "product_defect"
    if sr.startswith("同指纹熔断") or sr.startswith("补测轮次预算"):
        return "case_defect"      # 保守：无法判 env 时按 agent 责任侧（诚实保守）
    if "覆盖率" in sr and "预算耗尽" in sr:
        return "none"
    return "none" if vr != "stop" else None


def source_b_tmpdb() -> dict:
    """B 源：49 个归档临时库 → 工具成功率/循环步数/agent LLM 行。"""
    calls: dict[tuple, int] = {}
    agent_llm = 0
    steps_total = 0
    n_db = 0
    for dbp in sorted(glob.glob(TMPDB_GLOB)):
        try:
            conn = sqlite3.connect(f"file:{Path(dbp).as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "skill_invocations" not in tables:
                continue
            n_db += 1
            cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_invocations)")}
            if "name" in cols:
                q = ("SELECT s.name, i.status, COUNT(*) FROM skill_invocations i "
                     "JOIN skills s ON s.id=i.skill_id WHERE s.name LIKE 'test.%' "
                     "GROUP BY s.name, i.status")
                rows = conn.execute(q).fetchall()
            else:   # 旧 schema：skill_id 需 JOIN skills
                q = ("SELECT s.name, i.status, COUNT(*) FROM skill_invocations i "
                     "JOIN skills s ON s.id=i.skill_id WHERE s.name LIKE 'test.%' "
                     "GROUP BY s.name, i.status")
                rows = conn.execute(q).fetchall()
            for name, status, n in rows:
                calls[(name, status)] = calls.get((name, status), 0) + n
            if "llm_calls" in tables:
                agent_llm += conn.execute(
                    "SELECT COUNT(*) FROM llm_calls WHERE phase LIKE 'agent%'"
                ).fetchone()[0]
            if "agent_messages" in tables:
                steps_total += conn.execute(
                    "SELECT COUNT(*) FROM agent_messages WHERE content LIKE '[strategy]%' "
                    "OR content LIKE '[diagnose]%'").fetchone()[0]
        finally:
            conn.close()
    ok_n = sum(n for (_, st), n in calls.items() if st == "ok")
    total = sum(calls.values())
    return {"n_db": n_db, "calls_by": {f"{k[0]}|{k[1]}": v for k, v in sorted(calls.items())},
            "calls_total": total, "calls_ok": ok_n,
            "tool_success_rate": (ok_n / total) if total else None,
            "agent_llm_rows": agent_llm, "loop_msg_rows": steps_total}


def source_c_prod() -> dict:
    """C 源：生产库只读 → 响应时间近似（秒级）+ 缺失率 + 状态计数（不出完成率）。"""
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        n_tasks = conn.execute("SELECT COUNT(*) AS n FROM test_tasks").fetchone()["n"]
        status_counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM test_tasks GROUP BY status").fetchall()}
        fmt = "%Y-%m-%d %H:%M:%S"
        durs: list[float] = []
        missing = 0
        for r in conn.execute(
                "SELECT started_at, ended_at FROM agent_sessions "
                "WHERE started_at IS NOT NULL"):
            if not r["ended_at"]:
                missing += 1
                continue
            try:
                t4 = datetime.strptime(r["started_at"], fmt)
                t5 = datetime.strptime(r["ended_at"], fmt)
                durs.append(max((t5 - t4).total_seconds(), 0))
            except ValueError:
                missing += 1
        n_sessions = len(durs) + missing
        return {"n_tasks": n_tasks, "status_counts": status_counts,
                "sessions_total": n_sessions, "sessions_missing_ended": missing,
                "missing_rate": (missing / n_sessions) if n_sessions else None,
                "exec_duration_s_approx": _stats(durs),
                "note": "E9 失败语料实验主导 failed 计数——全库完成率混算无意义（设计稿 §11），不出数"}
    finally:
        conn.close()


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)
    mean = sum(vs) / n

    def pct(p):
        k = (n - 1) * p
        f = int(k)
        return vs[f] if f == k or f + 1 >= n else vs[f] + (vs[f + 1] - vs[f]) * (k - f)

    return {"n": n, "mean": round(mean, 2), "p50": round(pct(0.5), 2),
            "p95": round(pct(0.95), 2)}


def main() -> int:
    a = source_a_cells()
    b = source_b_tmpdb()
    cc = source_c_prod()
    out = {"generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "source_A_agent_loop_2x2": a, "source_B_tmpdb": b,
           "source_C_production": cc,
           "source_D_accuracy_reference": {
               "bench3_detection": "88.7%（63/71，三轮 189/213）",
               "reference": "docs/bench3三方基准实验报告-20261122.md §5（直接引用不重算）"}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outp = OUT_DIR / "metrics_v0.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("== A 源 48 格留档 ==")
    print(json.dumps(a, ensure_ascii=False, indent=1)[:600])
    print("== B 源 归档临时库 ==")
    print(json.dumps({k: v for k, v in b.items() if k != "calls_by"},
                     ensure_ascii=False, indent=1))
    print("== C 源 生产库（只读）==")
    print(json.dumps(cc, ensure_ascii=False, indent=1)[:600])
    print(f"\n[out] {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
