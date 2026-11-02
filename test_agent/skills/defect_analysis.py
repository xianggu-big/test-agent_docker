"""defect_analysis —— 失败结果的 AI 详细分析与修复建议（报告概览"判决先行"配套，2026-11-01）。

用户诉求：失败时在一个框里看到原因 + 详细分析 + 怎么修。
纪律（与 gen_report 同源）：**叙述可以 LLM，证据必须确定性**——
- 输入证据只来自：per-case 失败行（错误类+消息）、用例六要素（预期/输入/依据）、被测源码、需求；
- root_cause / fix 必须引用失败消息或源码符号，标 confidence（高/中/低），
  输出不可解析 → None（前端如实显示"分析不可用"，不硬造）；
- 分析是**推测**（oracle 问题：失败可能是产品缺陷也可能是用例预期错），文案里强制保留该口径；
- 结果缓存 test_reports.analysis（最新报告行），按需生成（按钮触发，llm_calls phase=defect_analysis）。
"""
import json
import logging
import re
from pathlib import Path

from ..skills.llm_client import make_chat

from .. import config
from .llm_usage import log_llm_call
from .prompt_library import DEFECT_ANALYST_SYSTEM

log = logging.getLogger(__name__)


def collect_failure_evidence(conn, task_id: int) -> dict:
    """确定性证据包：失败行 + 对应用例六要素 + 任务级归因 + 需求（纯 SQL，零 LLM）。"""
    rep = conn.execute("SELECT defects FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    try:
        defects = json.loads(rep["defects"]) if rep and rep["defects"] else {}
    except (TypeError, ValueError):
        defects = {}
    logs = [r["content"] for r in conn.execute(
        """SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id = m.session_id
           WHERE s.id = (SELECT id FROM agent_sessions WHERE task_id=?
                         ORDER BY id DESC LIMIT 1)
             AND m.content LIKE '[evaluate·failed] FAILED %' ORDER BY m.id""",
        (task_id,))]
    cases_by_name = {r["name"]: dict(r) for r in conn.execute(
        """SELECT c.name, c.expected, c.input, c.expected_basis, c.method, c.content
           FROM test_cases c JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=?""",
        (task_id,))}
    failed: list[dict] = []
    seen: set[str] = set()
    for line in logs:
        m = re.search(r"FAILED (\S+) - (.*)$", line)
        if not m:
            continue
        nodeid, msg = m.group(1), m.group(2)
        if nodeid in seen:
            continue
        seen.add(nodeid)
        base = nodeid.split("::")[-1].split("[")[0]
        case = cases_by_name.get(base, {})
        cls = re.match(r"([A-Za-z_]+)", msg)
        failed.append({"nodeid": nodeid, "name": base,
                       "error_class": cls.group(1) if cls else "Error",
                       "message": msg[:300],
                       "expected": case.get("expected"), "input": case.get("input"),
                       "expected_basis": case.get("expected_basis"),
                       "method": case.get("method"),
                       "code": (case.get("content") or "")[:500],
                       "origin": "generated" if base in cases_by_name else "repo"})
    req = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?", (task_id,)).fetchone()
    return {"failure_kind": defects.get("failure_kind"), "stop_reason": defects.get("stop_reason"),
            "failed": failed, "requirement": (req["requirement_text"] if req else "") or ""}


def build_analysis_prompt(evidence: dict, sources_text: str) -> str:
    rows = "\n".join(
        f"- 用例 {f['name']}（方法 {f.get('method') or '未标注'}；预期依据 {f.get('expected_basis') or '未标注'}）\n"
        f"  预期：{(f.get('expected') or '未标注')[:160]}\n"
        f"  输入：{(f.get('input') or '未标注')[:120]}\n"
        f"  实际失败：{f['error_class']}: {f['message'][:240]}\n"
        f"  用例代码：{f['code'][:360]}"
        for f in evidence["failed"][:12]) or "（无失败用例）"
    return f"""## 用户需求
{evidence['requirement'][:2000]}

## 任务级归因（系统确定性判定）
failure_kind={evidence.get('failure_kind')}；stop_reason={evidence.get('stop_reason')}

## 失败用例证据（逐条：预期 vs 实际）
{rows}

## 被测源码（供定位根因，可能已截断）
{sources_text[:7000]}

## 分析任务
对每条失败用例给出：
1. observed：实际发生了什么（只复述失败消息，不加戏）
2. expected：需求/用例要求的行为
3. root_cause：根因**推测**——必须指出源码中的具体函数/行为与失败消息的对应关系；
   如果更像是用例预期写错（而非产品缺陷），要明确说"疑似用例预期错误"并说明依据
4. fix：修复建议——产品缺陷给代码层面的具体改法（引用函数名/条件）；用例预期错给用例改法
5. confidence：高/中/低（证据充分=高；仅凭消息推断=低）
最后给 summary：一两句话总结这批失败的共同模式（如"全部集中在 clamp 的上界处理"）。
禁止编造证据之外的问题；禁止给出与失败消息无关的泛化建议。

## 输出格式
只输出一个 JSON 对象：
{{"summary": "…",
  "defects": [{{"case": "test_xxx", "observed": "…", "expected": "…",
     "root_cause": "…", "fix": "…", "confidence": "高"}}]}}"""


def parse_analysis(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    defects = data.get("defects")
    if not isinstance(defects, list):
        return None
    out = []
    for d in defects:
        if not isinstance(d, dict) or not d.get("case") or not d.get("fix"):
            continue
        out.append({k: str(d.get(k) or "")[:800] for k in
                    ("case", "observed", "expected", "root_cause", "fix", "confidence")})
    if not out and not data.get("summary"):
        return None
    return {"summary": str(data.get("summary") or "")[:600], "defects": out}


def run_defect_analysis(api_key: str, conn, task_id: int, ws: Path | None = None,
                        lang: str = "python") -> dict:
    """生成并缓存缺陷分析；无失败用例返回 {skipped}；LLM 不可解析返回 {error}（诚实不硬造）。"""
    evidence = collect_failure_evidence(conn, task_id)
    if not evidence["failed"]:
        return {"skipped": "无失败用例可分析", "evidence": evidence}
    sources_text = ""
    if ws and Path(ws).exists():
        from .gen_tests import _collect_sources
        sources_text = "\n\n".join(f"### {p}\n{t}" for p, t in _collect_sources(Path(ws), lang)[:6])
    llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                     timeout=120, max_retries=2)
    resp = llm.invoke([("system", DEFECT_ANALYST_SYSTEM),
                       ("user", build_analysis_prompt(evidence, sources_text))])
    log_llm_call(task_id, "defect_analysis", resp)
    parsed = parse_analysis(resp.content)
    if parsed is None:
        return {"error": "分析输出不可解析（诚实降级，未缓存）", "evidence": evidence}
    parsed["evidence_count"] = len(evidence["failed"])
    parsed["failure_kind"] = evidence.get("failure_kind")
    parsed["note"] = "root_cause/fix 为 AI 基于失败消息与源码的推测，需人工复核（oracle 问题：失败也可能是用例预期错）"
    conn.execute(
        """UPDATE test_reports SET analysis=? WHERE id=(SELECT id FROM test_reports WHERE task_id=?
           ORDER BY id DESC LIMIT 1)""", (json.dumps(parsed, ensure_ascii=False), task_id))
    conn.commit()
    return parsed
