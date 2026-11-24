# -*- coding: utf-8 -*-
"""四指标计算层（学院达标 T4，2026-11-25）：纯函数、零 LLM，全部从库重算。

口径唯一真源=docs/指标契约-主方向四指标-20261124.md（下称"契约"）。
- classify_outcome：契约 §1.1 的 outcome 五类判定（顺序先中先得）。
- tool_metrics：契约 §1.2 的工具调用结局七分类与两个成功率（宏/微平均并报）。
- hallucination_checks：契约 §1.3 的 C1/C2 机器可核对部分（C3 人工复核不在此层）。
- response_time：契约 §1.4 的排队+执行毫秒口径（历史近似走 *_legacy，approx 标）。
- summarize：n / mean±std / P50 / P95 / 缺失率。

指标诚实纪律（总入口硬纪律 7）：没数据返回 None（API 层转 null），不许报 0；
不同批次、不同口径不混算；历史近似值一律带 approx 标。
"""
from __future__ import annotations

import json
import statistics

# ---- 契约 §1.1 诚实出口签名（stop_reason 前缀）——来源锚点见契约 S8/S10/S13 ----
_HONEST_STOP_PREFIXES = (
    "API 型黑盒覆盖率不可达",        # S10 api_blackbox_stop（nodes.py api_blackbox_stop）
    "补测完成：非证人用例全部通过",  # S6 V8 补测完成变体（repair.py）
    "冻结证人",                      # S6 V8 预算耗尽变体
)
_HONEST_STOP_CONTAINS = (
    "未达准出标准（目标",            # S8 exit==0 覆盖率未达+预算耗尽（repair.py:239）
    "服务入口",                      # S13 L3 服务未找到
    "服务启动失败",                  # S13 L3 启动失败
)


def classify_outcome(*, run_status: str | None, task_status: str | None,
                     failure_kind: str | None, verdict: str | None,
                     passed: int = 0, failed: int = 0, has_report: bool = False,
                     stop_reason: str | None = None) -> str:
    """契约 §1.1：outcome 五类，按顺序先中先得。

    参数来源：run 行 + summary_json 冻结快照 + 任务行（本函数不查库——查库在
    outcome_for_run，纯判定逻辑在这里便于单测穷举）。
    """
    # 1) 用户取消（只报计数，API 层不进分母）
    if task_status == "cancelled" or run_status == "cancelled":
        return "not_completed_user"
    # 孤儿（图异常/进程被杀且无法归因）→ Agent 责任侧
    if run_status == "orphan":
        return "not_completed_agent"
    has_exec = (passed or 0) + (failed or 0) > 0
    kind = failure_kind or "none"
    # 2) 环境类 stop（契约修订：无论是否有执行）
    if verdict == "stop" and kind == "env":
        return "not_completed_env"
    # 3) 静态草稿（S11）：零容器执行但如实落盘设计草稿=诚实出口（契约明确，
    #    先于零执行 agent 责任分支判——否则静态模式全被冤判 not_completed_agent）
    if kind == "static":
        return "completed_with_limitation"
    # 4) Agent 责任侧未完成：零有效执行的非环境原因；或无报告行
    if not has_exec and verdict == "stop":
        return "not_completed_agent"
    if not has_report and run_status != "running":
        return "not_completed_agent"
    # 5) 正常出口：pass 达标；检出产品缺陷（目标达成）
    if verdict == "pass":
        return "completed"
    if kind == "product_defect":
        return "completed"
    # 6) 诚实出口：黑盒出口/覆盖率未达如实上报/L3 诚实降级
    reason = stop_reason or ""
    if any(reason.startswith(p) for p in _HONEST_STOP_PREFIXES):
        return "completed_with_limitation"
    if any(k in reason for k in _HONEST_STOP_CONTAINS) and (failed or 0) == 0:
        return "completed_with_limitation"
    # 有执行+报告但既非正常出口也非已知诚实出口（熔断/预算耗尽未获结论）
    return "not_completed_agent"


def outcome_for_run(conn, run_row) -> str | None:
    """从库取证后调 classify_outcome。run 未收口（running）返回 None——没跑完
    不进任何统计。快照缺失（旧数据/账本中途关闭）时退化用任务行。"""
    if run_row["status"] == "running":
        return None
    task = conn.execute("SELECT status FROM test_tasks WHERE id=?",
                        (run_row["task_id"],)).fetchone()
    snap: dict = {}
    if run_row["summary_json"]:
        try:
            snap = json.loads(run_row["summary_json"])
        except (TypeError, ValueError):
            snap = {}
    passed = failed = 0
    has_report = bool(isinstance(snap.get("report"), list) and snap["report"])
    failure_kind = None
    verdict = None
    report = snap.get("report")
    if has_report:
        latest = report[0] or {}
        passed = int(latest.get("passed") or 0)
        failed = int(latest.get("failed") or 0)
        try:
            defects = json.loads(latest.get("defects") or "{}")
            failure_kind = defects.get("failure_kind")
        except (TypeError, ValueError):
            pass
    # verdict 推导：快照 final.status 是可靠信号（report 节点 done ⟺ verdict=pass）；
    # defects 里 stop_reason 恒非空（pass 也写"达标"），不能当 stop 证据。
    final = snap.get("final") or {}
    if final.get("status") == "done":
        verdict = "pass"
    elif final.get("status") in ("failed", "cancelled"):
        verdict = "stop"
    case_counts = snap.get("case_counts") or {}
    if isinstance(case_counts, dict):
        passed = max(passed, int(case_counts.get("passed") or 0))
        failed = max(failed, int(case_counts.get("failed") or 0))
    verdict = verdict or snap.get("verdict")   # 手工快照兼容
    stop_reason = run_row["stop_reason"]
    return classify_outcome(
        run_status=run_row["status"], task_status=task["status"] if task else None,
        failure_kind=failure_kind, verdict=verdict,
        passed=passed or 0, failed=failed or 0, has_report=has_report,
        stop_reason=stop_reason)


# ---- 契约 §1.2：工具结局七分类 ----

_DENY_SUBCLASS_PREFIXES = (
    ("协议违规", "protocol"), ("call 缺 capability", "protocol"),
    ("能力不在菜单", "existence"),
    ("参数校验拒绝", "schema"),
    ("密钥键名越界", "secret"), ("能力声明了 secrets", "secret"),
    ("验证冻结任务仅允许 R0", "mode"), ("静态模式仅允许 R0", "mode"),
    ("R2 目录插件仅 normal", "governance"),
)


def deny_subclass(reason: str | None) -> str:
    """拒绝原因前缀 → 子类（契约 §1.2 推导表；七闸顺序=优先级）。"""
    r = reason or ""
    for prefix, sub in _DENY_SUBCLASS_PREFIXES:
        if r.startswith(prefix) or prefix in r:
            return sub
    if "需人工批准" in r or "永不自批" in r:
        return "governance"
    return "other"


def step_outcome(step_row) -> str:
    """单步 → 契约 §1.2 七类之一（finish 不计分母，单独由调用方计数）。"""
    if step_row["action_type"] == "unparseable":
        return "parse_error"
    if step_row["allowed"] == 0:
        return "denied_policy"
    if step_row["allowed"] is None:
        return "not_executed"
    status = step_row["result_status"]
    if status == "denied":
        return "denied_registry"
    if status in ("ok", "failed", "timeout"):
        return status
    return "failed"   # 未知执行态按失败计（诚实保守）


def tool_metrics_for_run(conn, run_id: int) -> dict:
    """单 run 的工具调用指标（契约 §1.2）。零调用 run → calls 全 0 且
    zero_call=True——不进宏平均分母，但必须如实出现在"零调用 run 数"。"""
    rows = conn.execute(
        "SELECT action_type, allowed, result_status, deny_reason FROM agent_steps "
        "WHERE run_id=? AND action_type != 'finish' ORDER BY step_no",
        (run_id,)).fetchall()
    counts = {"ok": 0, "failed": 0, "timeout": 0, "denied_policy": 0,
              "denied_registry": 0, "parse_error": 0, "not_executed": 0}
    deny_by: dict[str, int] = {}
    for r in rows:
        oc = step_outcome(r)
        counts[oc] += 1
        if oc == "denied_policy":
            sub = deny_subclass(r["deny_reason"])
            deny_by[sub] = deny_by.get(sub, 0) + 1
    initiated = sum(counts.values())
    executed = counts["ok"] + counts["failed"] + counts["timeout"]
    return {
        "calls": initiated, "zero_call": initiated == 0,
        "counts": counts,
        "deny_by_subclass": deny_by,
        "denied_by_fabrication": deny_by.get("existence", 0),
        "call_success_rate": (counts["ok"] / initiated) if initiated else None,
        "exec_success_rate": (counts["ok"] / executed) if executed else None,
        "deny_rate": ((counts["denied_policy"] + counts["denied_registry"]) / initiated)
        if initiated else None,
        "timeout_rate": (counts["timeout"] / initiated) if initiated else None,
    }


def tool_metrics_aggregate(conn, run_ids: list[int]) -> dict:
    """宏平均（run 等权）+ 微平均（池化全部调用）并报；零调用 run 数单列。"""
    per_run = [tool_metrics_for_run(conn, rid) for rid in run_ids]
    nonzero = [m for m in per_run if not m["zero_call"]]
    micro_counts = {k: sum(m["counts"][k] for m in per_run)
                    for k in ("ok", "failed", "timeout", "denied_policy",
                              "denied_registry", "parse_error", "not_executed")}
    micro_init = sum(micro_counts.values())
    micro_exec = micro_counts["ok"] + micro_counts["failed"] + micro_counts["timeout"]
    macro_call = ([m["call_success_rate"] for m in nonzero] or None) and \
        sum(m["call_success_rate"] for m in nonzero) / len(nonzero)
    return {
        "n_runs": len(run_ids), "n_zero_call_runs": len(per_run) - len(nonzero),
        "macro": {"call_success_rate": macro_call,
                  "exec_success_rate": (sum(m["exec_success_rate"] for m in nonzero)
                                        / len(nonzero)) if nonzero else None},
        "micro": {
            "counts": micro_counts,
            "call_success_rate": (micro_counts["ok"] / micro_init) if micro_init else None,
            "exec_success_rate": (micro_counts["ok"] / micro_exec) if micro_exec else None,
        },
        "per_run": per_run,
    }


# ---- 契约 §1.3：C1/C2 机器可核对（C3 人工复核在 T9 材料层） ----

def hallucination_checks_for_run(conn, run_id: int) -> dict:
    """C1 引用断言 + C2 工具断言的机器核对。finish 的 decision_json 里 diagnosis
    存在时核对 evidence_refs 序号；C2 从被拒步骤的 deny 子类取编造证据。"""
    c1_total = c1_invalid = 0
    c2_total = c2_invalid = 0
    max_step = conn.execute(
        "SELECT COALESCE(MAX(step_no), 0) AS m FROM agent_steps WHERE run_id=? "
        "AND action_type != 'finish'", (run_id,)).fetchone()["m"]
    for r in conn.execute(
            "SELECT action_type, allowed, deny_reason, decision_json FROM agent_steps "
            "WHERE run_id=? ORDER BY step_no", (run_id,)).fetchall():
        if r["action_type"] == "call":
            # C2：每个发起的调用都是一条工具断言（∈菜单+schema=策略闸存在闸/schema 闸）
            c2_total += 1
            if r["allowed"] == 0:
                sub = deny_subclass(r["deny_reason"])
                if sub in ("existence", "schema"):
                    c2_invalid += 1
        payload: dict = {}
        if r["decision_json"]:
            try:
                payload = json.loads(r["decision_json"])
            except (TypeError, ValueError):
                payload = {}
        diag = payload.get("diagnosis") or {}
        for ref in diag.get("evidence_refs") or []:
            c1_total += 1
            try:
                if not (1 <= int(ref) <= max_step):
                    c1_invalid += 1
            except (TypeError, ValueError):
                c1_invalid += 1
    denom = c1_total + c2_total
    return {
        "c1": {"total": c1_total, "invalid": c1_invalid},
        "c2": {"total": c2_total, "invalid": c2_invalid},
        "checkable_hallucination_rate": ((c1_invalid + c2_invalid) / denom)
        if denom else None,
    }


# ---- 契约 §1.4：响应时间（新数据毫秒） ----

def response_time_for_run(conn, run_row) -> dict | None:
    """排队+执行毫秒。run 未收口（ended_ms NULL）→ None（缺失如实）。"""
    if not run_row["started_ms"] or not run_row["ended_ms"]:
        return None
    exec_ms = run_row["ended_ms"] - run_row["started_ms"]
    queue_ms = None
    if run_row["queue_id"]:
        q = conn.execute("SELECT enqueued_at, started_at FROM exec_queue WHERE id=?",
                         (run_row["queue_id"],)).fetchone()
        if q and q["enqueued_at"] and q["started_at"]:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            try:
                t2 = datetime.strptime(q["enqueued_at"], fmt)
                t3 = datetime.strptime(q["started_at"], fmt)
                queue_ms = max(int((t3 - t2).total_seconds() * 1000), 0)
            except ValueError:
                queue_ms = None
    return {"exec_ms": exec_ms, "queue_ms": queue_ms,
            "total_ms": exec_ms + (queue_ms or 0)}


# ---- 历史近似（只给 T8 用；无 run 行的任务） ----

def _legacy_response_time(conn, task_id: int) -> dict | None:
    """历史近似（契约：历史秒级 + approx 标，不与新数据混算）。"""
    sess = conn.execute(
        "SELECT started_at, ended_at FROM agent_sessions WHERE task_id=? "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not sess or not sess["started_at"] or not sess["ended_at"]:
        return None
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        t4 = datetime.strptime(sess["started_at"], fmt)
        t5 = datetime.strptime(sess["ended_at"], fmt)
    except ValueError:
        return None
    return {"exec_s": max((t5 - t4).total_seconds(), 0), "approx": True}


# ---- 汇总 ----

def summarize(values: list[float], missing: int = 0) -> dict | None:
    """n / mean±std / P50 / P95 / 缺失率。零样本 → None（API 转 null，不报 0）。"""
    if not values:
        return None if missing == 0 else {"n": 0, "missing": missing, "value": None}
    vs = sorted(values)
    n = len(vs)

    def pct(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        return vs[f] if f == k or f + 1 >= n else vs[f] + (vs[f + 1] - vs[f]) * (k - f)

    mean = statistics.fmean(vs)
    std = statistics.pstdev(vs) if n > 1 else 0.0
    return {"n": n, "mean": mean, "std": std, "p50": pct(0.50), "p95": pct(0.95),
            "missing": missing,
            "missing_rate": (missing / (n + missing)) if (n + missing) else None}
