"""log_analyze —— 应用运行时日志的 LLM 摘要与异常解释（黑盒能力补齐阶段1）。

纪律（与 defect_analysis 同源）：叙述可以 LLM，证据必须确定性——
- 输入证据只来自 app_logs 结构化行（kind/level/line_no/message，纯 SQL 收集）；
- 叙述必须以「行 N」格式引用**真实存在**的行号（N ≤ 该轮日志总行数 log_lines）：
  引用不存在/超界行号 → 整份分析判失败（防 vibes 叙述，测试矩阵负例点名）；
  有错误条目却零引用 → 同样判失败（叙述必须锚定证据行）；
- 无日志/无错误行 → {skipped} 诚实降级；LLM 输出不可解析 → {error} 不硬造；
- 解释是推测不是结论，note 强制保留该口径（oracle 问题同 defect_analysis）。
"""
import json
import logging
import re

from ..skills.llm_client import make_chat

from .. import config
from .llm_usage import log_llm_call
from .prompt_library import LOG_ANALYST_SYSTEM

log = logging.getLogger(__name__)

# 引用格式铁律「行 N」：数字必须跟在"行"字后（提示词强制该格式，闸门只认该格式；
# 不做 L12/line 12 等宽匹配——宽匹配会把 L1/L2 级别名误读成行号，误杀合法叙述）
_CITE_RE = re.compile(r"行\s*(\d+)")


def validate_citations(text: str, valid_max_line: int) -> tuple[bool, list[int]]:
    """引用闸（纯函数）：叙述中全部「行 N」引用必须在 1..valid_max_line 内，
    且至少一条引用（零引用=叙述未锚定证据）。返回 (ok, 超界行号列表)。"""
    cites = [int(m) for m in _CITE_RE.findall(text or "")]
    if not cites:
        return False, []
    invalid = [c for c in cites if c < 1 or c > valid_max_line]
    return (not invalid), invalid


def collect_log_evidence(conn, task_id: int, round_no: int | None = None) -> dict:
    """确定性证据包（纯 SQL）：app_logs 全部轮次 → 取最新轮的条目集。"""
    q = "SELECT round_no, level, kind, line_no, message, raw_line, log_path, log_lines" \
        " FROM app_logs WHERE task_id=?"
    args: list = [task_id]
    if round_no is not None:
        q += " AND round_no=?"
        args.append(round_no)
    rows = [dict(r) for r in conn.execute(q + " ORDER BY round_no, line_no", args)]
    latest = rows[-1]["round_no"] if rows else None
    entries = [r for r in rows if r["round_no"] == latest] if latest is not None else []
    first = entries[0] if entries else {}
    return {"round_no": latest, "entries": entries,
            "log_lines": first.get("log_lines") or 0,
            "log_path": first.get("log_path")}


def build_log_analyze_prompt(evidence: dict) -> str:
    rows = "\n".join(f"- 行 {e['line_no']} [{e['level']}/{e['kind']}] {e['message']}"
                     for e in evidence["entries"][:40]) or "（无）"
    return f"""## 应用运行时日志证据（确定性解析自 app_logs，第 {evidence['round_no']} 轮）
日志共 {evidence['log_lines']} 行，命中错误/警告行 {len(evidence['entries'])} 条；
原文快照：{evidence.get('log_path') or '（未记录）'}

{rows}

## 分析任务
1. summary：这一轮应用日志反映的总体状况（1-3 句，只概括上面列出的证据）；
2. items：逐条解释错误/警告行——异常含义、可能根因（**推测，注明**）、与被测业务的关系。

## 引用纪律（硬性，违反即整份分析作废）
- 解释必须用「行 N」格式引用上面证据中的真实行号（1 ≤ N ≤ {evidence['log_lines']}）；
- 禁止编造证据之外的行号、事件或原因；不确定的写"证据不足"；
- 你的解释是推测，最终定性以用例执行结果与人工复核为准。

## 输出格式
只输出一个 JSON 对象：
{{"summary": "…", "items": [{{"line": 3, "explain": "行 3 是……"}}]}}"""


def parse_log_summary(text: str) -> dict | None:
    """LLM 输出 → {summary, items}；不可解析返回 None（诚实降级不硬造）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    summary = str(data.get("summary") or "")
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if not summary and not items:
        return None
    return {"summary": summary[:600],
            "items": [{k: str(i.get(k) or "")[:300] for k in ("line", "explain")}
                      for i in items[:20]]}


def run_log_analyze(api_key: str, conn, task_id: int, round_no: int | None = None) -> dict:
    """生成日志分析（不落缓存，调用方决定去向）；三态诚实降级：
    skipped=无证据 / error=不可解析或引用闸拒绝 / 正常={summary,items,citations_ok}。"""
    evidence = collect_log_evidence(conn, task_id, round_no)
    if not evidence["entries"]:
        return {"skipped": "该任务（轮）无应用日志错误/警告行可分析", "evidence": evidence}
    llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=api_key,
                    base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                    timeout=120, max_retries=2)
    resp = llm.invoke([("system", LOG_ANALYST_SYSTEM),
                       ("user", build_log_analyze_prompt(evidence))])
    log_llm_call(task_id, "log_analyze", resp)
    parsed = parse_log_summary(resp.content)
    if parsed is None:
        return {"error": "日志分析输出不可解析（诚实降级，不硬造）", "evidence": evidence}
    # 引用闸：summary + items 的行/explain 全文并入校验域（items.line 数字也校验）
    narrative = " ".join([parsed["summary"]] +
                         [f"行 {i.get('line')} {i.get('explain')}" for i in parsed["items"]])
    valid_max = evidence["log_lines"] or max(e["line_no"] for e in evidence["entries"])
    ok, invalid = validate_citations(narrative, valid_max_line=valid_max)
    if not ok:
        reason = (f"叙述引用了不存在/超界的行号：{invalid}（合法域 1..{valid_max}）"
                  if invalid else "叙述未引用任何「行 N」格式的证据行号（必须锚定证据）")
        log.warning("log_analyze 引用闸拒绝 task=%s round=%s: %s",
                    task_id, evidence["round_no"], reason)
        return {"citations_ok": False, "error": reason, "evidence": evidence}
    parsed["citations_ok"] = True
    parsed["round_no"] = evidence["round_no"]
    parsed["note"] = "摘要/解释为 AI 基于日志行的推测，原文见 app_logs 与工作区 logs/ 快照，最终定性以用例执行与人工复核为准"
    return parsed
