"""评估阶段五工具（vision-and-gaps §3 定稿）：collect_metrics / map_requirements /
gen_report / review_report / gen_charts。

纪律（论文"评估可信度"的地基）：
- 数字全部由确定性代码从 SQL / pytest 输出算出，LLM 只负责叙述（gen_report）与一致性复核（review_report 的 LLM 闸）
- review_report 规则闸先行（纯函数可单测），LLM 闸为辅；审查失败 fail-open（质量闸非安全闸，留痕放行）
- 全部走 SkillRegistry（skill_invocations 审计台账 → 论文"工具调用成功率"指标）
"""
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

from .. import config
from ..store.db import get_conn
from .llm_usage import log_llm_call
from .prompt_library import REPORT_SYSTEM, REVIEW_SYSTEM, build_map_prompt
from .registry import Skill

log = logging.getLogger(__name__)

# 兼容路径：老任务（evaluation_rounds 建表前）的轨迹解析自叙述消息（新数据走表，勿再依赖此格式）
_EVAL_MSG = re.compile(
    r"\[evaluate 第(\d+)轮\] 通过(\d+) 失败(\d+) 跳过(\d+) 覆盖率(\S+) 指纹\S* → (\w+)（")
_STOPWORDS = "的了在与是和或对为中断将把被个及其"
# 中文虚词单字：出现在 2-gram 中任一字则丢弃该 gram（降低噪声匹配）


# ---------------------------------------------------------------------------
# 1. collect_metrics —— 测试数据汇总（确定性，LLM 禁止自己算数）
# ---------------------------------------------------------------------------

def _missing_lines(spec: str) -> set[int]:
    """term-missing 的 missing 列（如 "12, 15-18, 30"）展开为行号集合。"""
    out: set[int] = set()
    for tok in (spec or "").replace(" ", "").split(","):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif tok.isdigit():
            out.add(int(tok))
    return out


def hotspot_coverage(hotspots: list[dict], per_file: list[dict]) -> dict:
    """W4 报告指标「复杂度热点未覆盖」（设计文档 §3-W4 ②，纯确定性求值）。

    交叉规则（per_file 只收有缺口的文件，B2 口径）：
    - 热点文件不在 per_file → 整文件已覆盖（无缺口证据即覆盖，只认正向证据）
    - 文件在列但热点起始行不在 missing 行集 → 已覆盖
    - 行命中 missing → 未覆盖
    返回 {total, uncovered, uncovered_list}；hotspots 为空时 total=0。
    """
    missing_by_file = {r["file"]: _missing_lines(r.get("missing") or "")
                       for r in per_file or []}
    uncovered: list[str] = []
    for h in hotspots or []:
        lines = missing_by_file.get(h["file"])
        if lines and h["lineno"] in lines:
            uncovered.append(f"{h['file']}::{h['name']}(V(G)={h['complexity']})")
    return {"total": len(hotspots or []), "uncovered": len(uncovered),
            "uncovered_list": uncovered}


def collect_metrics(conn, task_id: int, report_id: int | None = None) -> dict:
    """从 SQL 汇总结构化指标包。无报告行时返回 no_report=True（诚实短路）。"""
    rep_sql = ("SELECT * FROM test_reports WHERE task_id=?"
               + (" AND id=?" if report_id else "") + " ORDER BY id DESC LIMIT 1")
    rep = conn.execute(rep_sql, (task_id, report_id) if report_id else (task_id,)).fetchone()
    if not rep:
        return {"no_report": True, "task_id": task_id}
    task = conn.execute("SELECT status, coverage_target, requirement_text FROM test_tasks"
                        " WHERE id=?", (task_id,)).fetchone()

    passed, failed, skipped = rep["passed"], rep["failed"], rep["skipped"]
    denom = passed + failed + skipped
    metrics = {
        "task_id": task_id,
        "report_id": rep["id"],
        "task_status": task["status"] if task else None,
        "coverage_target": task["coverage_target"] if task else None,
        "passed": passed, "failed": failed, "skipped": skipped,
        "pass_rate": round(passed / denom, 4) if denom else None,
        "coverage_line": rep["coverage_line"],
        "duration_ms": rep["duration_ms"],
        "failure_kind": (json.loads(rep["defects"]) or {}).get("failure_kind")
                        if rep["defects"] else None,
        "stop_reason": (json.loads(rep["defects"]) or {}).get("stop_reason")
                       if rep["defects"] else None,
    }

    # R-Loop 轮次轨迹（P1-③ 根修）：结构化表 evaluation_rounds 为唯一真源；
    # 老任务（建表前跑的实验数据）无表行时回退解析叙述消息，兼容历史数据。
    trace = []
    rows = conn.execute("""SELECT round_no, passed, failed, skipped, coverage_line, verdict
                           FROM evaluation_rounds WHERE task_id=? AND level='L1'
                           ORDER BY id""",   # 级别过滤：L2 轮次不混入 L1 轨迹/趋势图
                        (task_id,)).fetchall()
    if rows:
        metrics["trace_source"] = "evaluation_rounds"
        for r in rows:
            trace.append({"round": r["round_no"], "passed": r["passed"],
                          "failed": r["failed"], "skipped": r["skipped"],
                          "coverage": r["coverage_line"], "verdict": r["verdict"]})
    else:
        metrics["trace_source"] = "legacy_messages"
        mrows = conn.execute("""SELECT m.content FROM agent_messages m
                                JOIN agent_sessions s ON s.id = m.session_id
                                WHERE s.task_id = ? AND m.content LIKE '[evaluate 第%'
                                ORDER BY s.id, m.seq""", (task_id,))
        for r in mrows:
            if m := _EVAL_MSG.match(r["content"]):
                cov = m.group(5)
                trace.append({"round": int(m.group(1)), "passed": int(m.group(2)),
                              "failed": int(m.group(3)), "skipped": int(m.group(4)),
                              "coverage": None if cov in ("N/A", "None") else float(cov),
                              "verdict": m.group(6)})
    metrics["trace"] = trace
    metrics["verdict_counts"] = dict(Counter(t["verdict"] for t in trace))

    # 工具调用成功率（skill_invocations 审计台账）
    inv = conn.execute("""SELECT sk.name, i.status, COUNT(*) AS n, SUM(i.duration_ms) AS ms
                          FROM skill_invocations i JOIN skills sk ON sk.id = i.skill_id
                          WHERE i.task_id = ? GROUP BY sk.name, i.status""", (task_id,))
    by_skill: dict[str, dict] = {}
    for r in inv:
        d = by_skill.setdefault(r["name"], {"ok": 0, "total": 0, "duration_ms": 0})
        d["total"] += r["n"]
        d["duration_ms"] += r["ms"] or 0
        if r["status"] == "ok":
            d["ok"] += r["n"]
    for d in by_skill.values():
        d["success_rate"] = round(d["ok"] / d["total"], 4) if d["total"] else None
    metrics["tool_invocations"] = by_skill
    total = [d["total"] for d in by_skill.values()]
    ok = [d["ok"] for d in by_skill.values()]
    metrics["tool_success_rate"] = round(sum(ok) / sum(total), 4) if sum(total) else None

    # 用例统计（六要素完整率 = expected 非空比例；来源分布 builtin/ai/imported）
    cases = conn.execute("""SELECT c.source, COUNT(*) AS n,
                                   SUM(CASE WHEN c.expected IS NOT NULL
                                            AND c.expected != '' THEN 1 ELSE 0 END) AS with_exp
                            FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
                            WHERE s.task_id = ? GROUP BY c.source""", (task_id,))
    case_rows = [dict(r) for r in cases]
    metrics["cases_total"] = sum(r["n"] for r in case_rows)
    metrics["cases_by_source"] = {r["source"]: r["n"] for r in case_rows}
    metrics["six_elem_rate"] = (
        round(sum(r["with_exp"] or 0 for r in case_rows) / metrics["cases_total"], 4)
        if metrics["cases_total"] else None)

    # 四级双轨（蓝图 §3.4 指标契约）：任务带 L2 leg 时并入本级指标——端点覆盖/契约违背/
    # 需求级失败全来自确定性台账（test_levels.summary 由 L2 evaluate 节点写）；无 leg 整体缺席
    leg = conn.execute("SELECT status, verdict, summary FROM test_levels"
                       " WHERE task_id=? AND level='L2'", (task_id,)).fetchone()
    if leg:
        l2 = {"status": leg["status"], "verdict": leg["verdict"]}
        if leg["summary"]:
            l2.update(json.loads(leg["summary"]))
        metrics["l2"] = l2

    # L3 场景按级指标（D5 半自动）：无场景行整体缺席，叙述如实说无数据
    try:
        from ..skills.scenario_gen import l3_metrics
        l3m = l3_metrics(conn, task_id)
    except Exception:
        l3m = None
    if l3m:
        metrics["l3"] = l3m

    # W4 复杂度热点 × 覆盖交叉（读取时确定性求值，不回写表）：老任务无热点行
    # （建表前的实验数据 / 非 python 链路）→ 三字段整体不出现，叙述如实说无数据
    hs_rows = [dict(r) for r in conn.execute(
        """SELECT file, name, complexity, rank, lineno FROM complexity_hotspots
           WHERE task_id=? ORDER BY complexity DESC""", (task_id,))]
    if hs_rows:
        cross = hotspot_coverage(hs_rows, json.loads(rep["per_file"]) if rep["per_file"] else [])
        metrics["hotspots_total"] = cross["total"]
        metrics["hotspots_uncovered"] = cross["uncovered"]
        metrics["hotspots_uncovered_list"] = cross["uncovered_list"]

    # LLM 调用成本（P1-E 台账，全链路 7 个调用点）：老任务无行（建表前实验数据 /
    # 全程无 Key 的确定性降级链路）→ 四字段整体不出现，叙述如实说无数据
    lc = conn.execute(
        """SELECT COUNT(*) AS calls, SUM(prompt_tokens) AS p,
                  SUM(completion_tokens) AS c, SUM(total_tokens) AS t
           FROM llm_calls WHERE task_id=?""", (task_id,)).fetchone()
    if lc and lc["calls"]:
        metrics["llm_call_count"] = lc["calls"]
        metrics["llm_prompt_tokens"] = lc["p"]
        metrics["llm_completion_tokens"] = lc["c"]
        metrics["llm_total_tokens"] = lc["t"]
    return metrics


# ---------------------------------------------------------------------------
# 2. map_requirements —— RTM 矩阵（条件 ↔ 用例 ↔ 状态）
# ---------------------------------------------------------------------------

def _grams(text: str) -> set[str]:
    """中文 2-gram 特征：去空白与数字后取相邻二字组，含虚词/数字的 gram 丢弃。"""
    t = re.sub(r"[\s\d，。；：、（）()【】\[\]\"'`\-—_/\\.,;:!?]", "", text or "")
    return {a + b for a, b in zip(t, t[1:])
            if a not in _STOPWORDS and b not in _STOPWORDS} or set()


def _heuristic_map(conditions: list[dict], cases: list[dict],
                   threshold: float = 0.35) -> dict[int, list[str]]:
    """确定性映射：条件 2-gram 在用例文本中的命中率 ≥ 阈值视为覆盖。"""
    case_grams = {c["name"]: _grams(c["name"] + (c.get("expected") or "") + (c.get("content") or ""))
                  for c in cases}
    out: dict[int, list[str]] = {}
    for cond in conditions:
        cg = _grams(cond["condition"])
        hits = []
        if cg:
            for c in cases:
                overlap = len(cg & case_grams[c["name"]])
                if overlap / len(cg) >= threshold:
                    hits.append(c["name"])
        out[cond["id"]] = hits
    return out


def map_requirements(conn, task_id: int, api_key: str | None = None) -> dict:
    """条件 ↔ 用例 映射：LLM 辅助（配置 Key 时）→ 失败/无 Key 降级确定性启发式。
    产出落 rtm_maps（每次评估覆盖旧快照）。"""
    conditions = [dict(r) for r in conn.execute(
        "SELECT id, requirement_ref, condition, priority FROM test_conditions"
        " WHERE task_id=? ORDER BY id", (task_id,))]
    cases = [dict(r) for r in conn.execute(
        """SELECT c.name, c.expected, c.content FROM test_cases c
           JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.enabled=1""", (task_id,))]

    source, mapping, note = "heuristic", None, ""
    if conditions and cases and api_key:
        try:
            from langchain_openai import ChatOpenAI  # 延迟导入，无 Key 时零开销
            llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                             base_url=config.DEEPSEEK_BASE_URL, temperature=0,
                             timeout=60, max_retries=2)
            resp = llm.invoke([("system",
                                "你是测试分析员。只依据语义对应关系建立映射。只输出 JSON。"),
                               ("user", build_map_prompt(conditions, cases))])
            log_llm_call(task_id, "map_rtm", resp)
            data = json.loads(re.search(r"\{.*\}", resp.content, re.S).group(0))
            valid_names = {c["name"] for c in cases}
            mapping = {c["id"]: [n for n in (m.get("cases") or []) if n in valid_names]
                       for m in data.get("mappings", [])
                       for c in conditions if c["id"] == m.get("condition_id")}
            for c in conditions:            # LLM 漏答的条件补空
                mapping.setdefault(c["id"], [])
            source = "llm"
        except Exception as e:              # noqa: BLE001 —— LLM 映射失败诚实降级
            note = f"llm 映射失败降级 heuristic：{type(e).__name__}"
            log.warning("map_requirements LLM 降级: %s", e)
    if mapping is None:
        mapping = _heuristic_map(conditions, cases)

    matrix = [{**c, "cases": mapping.get(c["id"], []),
               "status": "covered" if mapping.get(c["id"]) else "gap"}
              for c in conditions]
    latest = conn.execute("SELECT id FROM test_reports WHERE task_id=?"
                          " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    conn.execute("DELETE FROM rtm_maps WHERE task_id=?", (task_id,))
    conn.execute("INSERT INTO rtm_maps(task_id, report_id, source, matrix) VALUES (?,?,?,?)",
                 (task_id, latest["id"] if latest else None, source,
                  json.dumps(matrix, ensure_ascii=False)))
    conn.commit()
    covered = sum(1 for m in matrix if m["status"] == "covered")
    return {"task_id": task_id, "source": source, "note": note,
            "conditions_total": len(conditions), "covered": covered,
            "gap": len(conditions) - covered, "matrix": matrix}


# ---------------------------------------------------------------------------
# 3. gen_report —— 报告叙述（模板固定；数字禁造；无 Key 走确定性模板）
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "N/A"


def fallback_report(metrics: dict, rtm: dict) -> str:
    """无 LLM Key 的确定性报告（数字与指标包逐一相等，可被规则闸校验）。"""
    cov = metrics.get("coverage_line")
    tgt = metrics.get("coverage_target")
    lines = [
        f"# 测试报告（任务 {metrics['task_id']}）", "",
        "## 结论",
        f"本轮通过 {metrics['passed']}、失败 {metrics['failed']}、跳过 {metrics['skipped']}，"
        f"通过率 {metrics['pass_rate'] if metrics['pass_rate'] is not None else 'N/A'}；"
        f"行覆盖率 {f'{cov:.0%}' if cov is not None else 'N/A'}"
        f"（准出标准 {f'{tgt:.0%}' if tgt is not None else '未冻结'}）。",
        f"终态：{metrics.get('task_status')} —— {metrics.get('stop_reason') or '达标'}", "",
        "## 缺陷与风险",
    ]
    if metrics.get("failure_kind") == "product_defect":
        lines.append("- 疑似产品缺陷：断言失败用例保留为证人，需人工定性（feedback_annotations）。")
    gap_rows = [m for m in rtm.get("matrix", []) if m["status"] == "gap"]
    if gap_rows:
        lines.append("- RTM 覆盖缺口（需求拆解出的条件尚无用例覆盖）：")
        lines += [f"  - {m['condition'][:60]}" for m in gap_rows[:5]]
    if metrics.get("hotspots_total"):
        u, t = metrics["hotspots_uncovered"], metrics["hotspots_total"]
        lines.append(f"- 复杂度热点未覆盖 {u}/{t}：" +
                     ("; ".join(metrics["hotspots_uncovered_list"][:3])
                      if u else "热点所在行均已覆盖"))
    if metrics.get("llm_total_tokens") is not None:
        lines.append(f"- LLM 调用 {metrics['llm_call_count']} 次、token 消耗 "
                     f"{metrics['llm_prompt_tokens']}/{metrics['llm_completion_tokens']}"
                     f"/{metrics['llm_total_tokens']}（输入/输出/合计）")
    if not lines[-1].startswith(("  -", "-")):
        lines.append("- 无。")
    return "\n".join(lines) + "\n"


def gen_report(conn, task_id: int, metrics: dict, rtm: dict,
               api_key: str | None = None) -> str:
    """生成叙述正文并落 test_reports.narrative。LLM 版输出 JSON 包裹防废话头。"""
    text = None
    if api_key:
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                             base_url=config.DEEPSEEK_BASE_URL, temperature=0.2,
                             timeout=60, max_retries=2)
            prompt = f"""## 指标包（数字唯一来源，禁止出现数据之外的数字）
{json.dumps({k: v for k, v in metrics.items() if k != "matrix"}, ensure_ascii=False)}

## RTM 矩阵
{json.dumps(rtm.get("matrix", []), ensure_ascii=False)}

## 任务
按固定模板写 markdown 测试报告：①结论（通过率/覆盖率/终态）②四阶段回溯（分析→设计→执行→评估，
引用 trace 轨迹与 tool_invocations 耗时）③缺陷清单（failure_kind=product_defect 时如实列出，
不得写成已解决）④风险与建议（RTM gap 条件、复杂度热点未覆盖、未达标原因；指标包无
hotspots_total 字段时写"无复杂度热点数据"，不得编造；无 llm_total_tokens 字段时
写"无 LLM 成本数据"，同样不得编造）。
提及数字时用固定表述：通过 N、失败 N、跳过 N、覆盖率 P%、复杂度热点未覆盖 M/N、
token 消耗 输入P/输出C/合计T。
只输出 JSON：{{"report": "markdown 全文"}}"""
            resp = llm.invoke([("system", REPORT_SYSTEM), ("user", prompt)])
            log_llm_call(task_id, "gen_report", resp)
            text = json.loads(re.search(r"\{.*\}", resp.content, re.S).group(0))["report"]
        except Exception as e:              # noqa: BLE001
            log.warning("gen_report LLM 失败，降级确定性模板: %s", e)
    if not text:
        text = fallback_report(metrics, rtm)
    conn.execute("UPDATE test_reports SET narrative=? WHERE id=?",
                 (text, metrics["report_id"]))
    conn.commit()
    return text


# ---------------------------------------------------------------------------
# 4. review_report —— 报告二次审查（规则闸纯函数先行 + LLM 闸为辅）
# ---------------------------------------------------------------------------

def review_rules(narrative: str, metrics: dict, verdict: str) -> list[str]:
    """规则闸（纯函数，可单测）：返回违规原因列表（空 = 通过）。"""
    reasons: list[str] = []
    cov = metrics.get("coverage_line")
    if cov is not None and not (0 <= cov <= 1):
        reasons.append(f"覆盖率越界：{cov} 不在 [0,1]")
    # 数字逐项比对：只校验叙述中实际出现的固定表述（(?<!未)防"未通过"误抓）。
    # L2 感知：指标包含 l2 按级指标（蓝图 §3.4）时，叙述可能引用本级数字——
    # L1/L2 两级的通过/失败/跳过都算合法数据源，避免"L2 层通过 11"被误判造假。
    l2 = metrics.get("l2") or {}

    def _legal(*vals) -> set:
        return {v for v in (metrics.get(vals[0]), l2.get(vals[0])) if v is not None}

    for pat, key in ((r"(?<!未)通过\s*(\d+)", "passed"),
                     (r"失败\s*(\d+)", "failed"),
                     (r"跳过\s*(\d+)", "skipped")):
        legal = _legal(key)
        if not legal:
            continue
        for m in re.findall(pat, narrative or ""):
            if int(m) not in legal:
                # P3-1：显示正则字面量本身（"通过"/"失败"），不再用 pat[1:3] 截出正则碎片
                label = pat.split("(")[0].replace("(?<!未)", "")
                reasons.append(f"叙述数字与数据不符：表述“{label}”写了 {m}，"
                               f"数据为 {sorted(legal)}（L1/L2 均不匹配）")
                break
    # P1-3（2026-10-31 体检 EXP1-A 实锤）："通过率 100%"这类比率措辞可绕过计数比对——
    # 出现即与 pass_rate 白名单比对（容忍 0.5% 舍入）；分数形式"通过率 x/y"换算后同检；
    # pass_rate 缺席时任何具体通过率都不可信
    pr = metrics.get("pass_rate")
    rate_forms = re.findall(r"通过率\s*([\d.]+)\s*%", narrative or "") + [
        f"{int(a) / int(b) * 100:.1f}"
        for a, b in re.findall(r"通过率\s*(\d+)\s*/\s*(\d+)", narrative or "")]
    for m in rate_forms:
        if pr is None:
            reasons.append("叙述给出具体通过率，但指标包无 pass_rate（无数据不得编造）")
            break
        if abs(float(m) - round(pr * 100)) > 0.5:
            reasons.append(f"叙述通过率 {m}% 与数据 {round(pr * 100)}% 不符")
            break
    if cov is not None:
        for m in re.findall(r"覆盖率\s*([\d.]+)\s*%", narrative or ""):
            if abs(float(m) - round(cov * 100)) > 0.5:
                reasons.append(f"叙述覆盖率 {m}% 与数据 {cov:.0%} 不符")
                break
    # W4 热点未覆盖 M/N（叙述提及才校验，与通过/失败数同款"出现即比对"语义）
    hs_u, hs_t = metrics.get("hotspots_uncovered"), metrics.get("hotspots_total")
    if hs_u is not None and hs_t:
        for m in re.findall(r"热点未覆盖\s*(\d+)\s*/\s*(\d+)", narrative or ""):
            if (int(m[0]), int(m[1])) != (hs_u, hs_t):
                reasons.append(f"叙述热点未覆盖 {m[0]}/{m[1]} 与数据 {hs_u}/{hs_t} 不符")
                break
    # LLM token 消耗 输入/输出/合计（P1-E 成本口径，同款"出现即比对"）
    llm_p, llm_c, llm_t = (metrics.get("llm_prompt_tokens"),
                           metrics.get("llm_completion_tokens"),
                           metrics.get("llm_total_tokens"))
    if llm_t is not None:
        for m in re.findall(r"token\s*消耗\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)",
                            narrative or ""):
            if (int(m[0]), int(m[1]), int(m[2])) != (llm_p, llm_c, llm_t):
                reasons.append(
                    f"叙述 token 消耗 {m[0]}/{m[1]}/{m[2]} 与数据 "
                    f"{llm_p}/{llm_c}/{llm_t} 不符")
            break
    if (verdict == "stop" or metrics.get("failure_kind") == "product_defect") \
            and re.search(r"已解决|全部通过|全部修复|均已修复|无缺陷|未发现缺陷|无遗留|验证完毕|均已验证",
                          narrative or ""):
        reasons.append("归因为疑似产品缺陷，报告却称已解决/无缺陷/无遗留（防造假红线）")
    if metrics.get("failed", 0) > 0 and re.search(r"全部通过", narrative or ""):
        reasons.append(f"数据存在 {metrics['failed']} 条失败，报告却称全部通过")
    return reasons


def review_report(conn, task_id: int, report_id: int, narrative: str,
                  metrics: dict, verdict: str, api_key: str | None = None) -> dict:
    """双闸审查：规则闸先行；LLM 闸复核一致性与证据。结论落 test_reports.review。"""
    reasons = review_rules(narrative, metrics, verdict)
    reviewer = "rules"
    if not reasons and api_key:
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                             base_url=config.DEEPSEEK_BASE_URL, temperature=0,
                             timeout=60, max_retries=2)
            resp = llm.invoke([("system", REVIEW_SYSTEM), ("user", f"""## 报告叙述
{narrative}

## 原始指标包
{json.dumps({k: v for k, v in metrics.items() if k != "matrix"}, ensure_ascii=False)}

## 任务
审查叙述与数据是否一致、结论是否有证据支撑。
审查聚焦：通过/失败/跳过数、覆盖率、终态与归因（product_defect 是否被如实上报）是否与数据一致、
结论是否有指标证据。tool_invocations 与 duration 属评估工具运行中台账，统计时点允许自然漂移，
不要因此判不一致。只输出 JSON：
{{"consistent": true/false, "reasons": ["不一致点"]}}""")])
            log_llm_call(task_id, "review_report", resp)
            data = json.loads(re.search(r"\{.*\}", resp.content, re.S).group(0))
            reviewer = "rules+llm"
            if not data.get("consistent"):
                reasons = [str(r) for r in (data.get("reasons") or [])] or ["LLM 审查判定不一致"]
        except Exception as e:              # noqa: BLE001 —— LLM 闸失败规则闸兜底
            log.warning("review_report LLM 闸跳过: %s", e)
    result = {"status": "reject" if reasons else "pass", "reasons": reasons,
              "reviewer": reviewer}
    conn.execute("UPDATE test_reports SET review=? WHERE id=?",
                 (json.dumps(result, ensure_ascii=False), report_id))
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# 5. gen_charts —— matplotlib 出图（论文插图直接产出）
# ---------------------------------------------------------------------------

def _setup_plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                       "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def gen_charts(task_id: int, metrics: dict, rtm: dict,
               out_dir: Path | None = None) -> list[str]:
    """四图：RTM 覆盖 / 覆盖率趋势 / 判定分布 / 工具耗时。返回文件路径列表。"""
    out_dir = out_dir or config.DATA_DIR / "reports" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    plt = _setup_plt()
    made: list[str] = []

    matrix = rtm.get("matrix") or []
    if matrix:
        fig, ax = plt.subplots(figsize=(8, max(2, 0.4 * len(matrix))))
        rows = matrix[::-1]
        ax.barh([m["condition"][:22] for m in rows],
                [1] * len(rows),
                color=["#4caf50" if m["status"] == "covered" else "#e53935" for m in rows])
        ax.set_title(f"Task {task_id}: RTM requirement coverage")
        ax.set_xlabel("covered (green) / gap (red)")
        ax.set_yticks(ax.get_yticks())
        ax.set_xticks([])
        p = out_dir / f"task_{task_id}_rtm.png"
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        made.append(str(p))

    trace = metrics.get("trace") or []
    if trace:
        xs = [t["round"] for t in trace]
        ys = [t["coverage"] * 100 if t["coverage"] is not None else 0 for t in trace]
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot(xs, ys, marker="o")
        tgt = metrics.get("coverage_target")
        if tgt:
            ax.axhline(tgt * 100, ls="--", c="gray", label=f"target {tgt:.0%}")
            ax.legend()
        ax.set_title(f"Task {task_id}: coverage by R-Loop round")
        ax.set_xlabel("round")
        ax.set_ylabel("line coverage %")
        p = out_dir / f"task_{task_id}_coverage_trend.png"
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        made.append(str(p))

        if metrics.get("verdict_counts"):
            fig, ax = plt.subplots(figsize=(5, 4))
            ax.pie(list(metrics["verdict_counts"].values()),
                   labels=list(metrics["verdict_counts"]),
                   autopct="%1.0f%%")
            ax.set_title(f"Task {task_id}: R-Loop verdict distribution")
            p = out_dir / f"task_{task_id}_verdicts.png"
            fig.tight_layout()
            fig.savefig(p, dpi=150)
            plt.close(fig)
            made.append(str(p))

    inv = metrics.get("tool_invocations") or {}
    if inv:
        fig, ax = plt.subplots(figsize=(7, 4))
        names = list(inv)
        ax.bar(names, [inv[n]["duration_ms"] for n in names],
               color=["#4caf50" if inv[n]["ok"] == inv[n]["total"] else "#ff9800" for n in names])
        ax.set_title(f"Task {task_id}: tool time (green = all ok)")
        ax.set_ylabel("duration ms")
        ax.tick_params(axis="x", rotation=20)
        p = out_dir / f"task_{task_id}_tools.png"
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        made.append(str(p))
    return made


# ---------------------------------------------------------------------------
# Skill 包装层（func 签名 (task_id, args) -> str）
# 注意：SkillRegistry 在线程池里执行 func —— 连接必须在 func 内部新建（sqlite3
# 禁止跨线程复用连接），不能闭包捕获主线程连接（单测实锤的坑）
# ---------------------------------------------------------------------------

def build_eval_skills() -> dict[str, Skill]:
    def _collect(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            return json.dumps(collect_metrics(conn, task_id, args.get("report_id")),
                              ensure_ascii=False)
        finally:
            conn.close()

    def _map(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            use_llm = args.get("use_llm", True)
            return json.dumps(map_requirements(
                conn, task_id, os.environ.get("DEEPSEEK_API_KEY") if use_llm else None),
                ensure_ascii=False)
        finally:
            conn.close()

    def _report(task_id: int, args: dict) -> str:
        # 快照一致性（run46 实锤的坑）：gen_report 与 review_report 必须看同一份指标包——
        # 各自现 collect 的话，评估工具自身的调用台账在两次 collect 之间会增长，
        # LLM 叙述引用旧数字、审查拿新数字比对 → 永远可能"自审自 reject"。
        # 所以 review 节点 collect 一次经 args 下传；独立调用本 skill 时才自取。
        conn = get_conn()
        try:
            metrics = args.get("metrics")
            if metrics is None:
                metrics = collect_metrics(conn, task_id)
            rtm = args.get("rtm")
            if rtm is None:
                latest = conn.execute("SELECT matrix FROM rtm_maps WHERE task_id=?"
                                      " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                rtm = {"matrix": json.loads(latest["matrix"]) if latest else []}
            text = gen_report(conn, task_id, metrics, rtm,
                              os.environ.get("DEEPSEEK_API_KEY"))
            return json.dumps({"narrative_chars": len(text)}, ensure_ascii=False)
        finally:
            conn.close()

    def _review(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            metrics = args.get("metrics")
            if metrics is None:
                metrics = collect_metrics(conn, task_id)
            if metrics.get("no_report"):
                return json.dumps({"status": "pass", "reasons": ["无报告数据，审查诚实跳过"],
                                   "reviewer": "skip"}, ensure_ascii=False)
            rep = conn.execute("SELECT narrative FROM test_reports WHERE id=?",
                               (metrics["report_id"],)).fetchone()
            result = review_report(conn, task_id, metrics["report_id"],
                                   rep["narrative"] or "", metrics,
                                   args.get("verdict", ""), os.environ.get("DEEPSEEK_API_KEY"))
            return json.dumps(result, ensure_ascii=False)
        finally:
            conn.close()

    def _charts(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            # 单快照语义（与 _report/_review 同款，run46 根修）：优先吃下传快照，
            # 独立调用本 skill 时才自取——否则图表数字与叙述/审查所依据的指标包
            # 在"评估工具自身台账"上漂移，报告页出现叙述与图表各说各话。
            metrics = args.get("metrics")
            if metrics is None:
                metrics = collect_metrics(conn, task_id)
            rtm = args.get("rtm")
            if rtm is None:
                latest = conn.execute("SELECT matrix FROM rtm_maps WHERE task_id=?"
                                      " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                rtm = {"matrix": json.loads(latest["matrix"]) if latest else []}
            paths = gen_charts(task_id, metrics, rtm)
            return json.dumps({"charts": paths}, ensure_ascii=False)
        finally:
            conn.close()

    return {
        "collect_metrics": Skill(
            name="collect_metrics", description="从 SQL 汇总测试指标包（确定性，LLM 禁止算数）",
            func=_collect, risk_level="low", timeout_ms=30_000, arg_schema={}),
        "map_requirements": Skill(
            name="map_requirements", description="RTM 矩阵：测试条件↔用例映射与缺口（LLM 辅助+启发式兜底）",
            func=_map, risk_level="low", timeout_ms=120_000,
            arg_schema={"use_llm": "bool"}),
        "gen_report": Skill(
            name="gen_report", description="按固定模板生成报告叙述（数字只能来自指标包）",
            func=_report, risk_level="low", timeout_ms=120_000, arg_schema={}),
        "review_report": Skill(
            name="review_report", description="报告二次审查双闸（规则先行+LLM 复核）",
            func=_review, risk_level="low", timeout_ms=120_000,
            arg_schema={"verdict": "str"}),
        "gen_charts": Skill(
            name="gen_charts", description="matplotlib 出图：RTM/覆盖率趋势/判定分布/工具耗时",
            func=_charts, risk_level="low", timeout_ms=60_000, arg_schema={}),
    }
