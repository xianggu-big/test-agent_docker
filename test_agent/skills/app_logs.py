"""应用运行时日志分档（黑盒能力补齐阶段1，docs/黑盒能力补齐任务包-20261106.md）。

统一五步模子的①②④⑤层：采集（容器内重定向 /work/logs/app.log + docker logs 兜底，
runner.read_app_log_pair 采集层）→ 确定性提取（四语言错误行正则）→ 结构化落库
（app_logs 第 28 表）→ API 呈现（GET /api/tasks/{tid}/app_logs?round=N——/logs 被既有 api_logs 占用，终验偏差入档）。
LLM 只在姐妹模块 log_analyze 做摘要且必须引用 line_no——本模块零 LLM。

设计纪律（任务包 §0 语言差异规范化）：解析正则只认**输出排版事实**
（Python Traceback 头 / Java at-帧与 Caused by / Go panic 与 goroutine / Node
rejection 字样），不写"某语言容易出某类问题"的经验规则；语言差异全部吸收在
本模块的确定性正则分支里，prompt 层零语言信息。

诚实边界：app_logs 只存错误/警告行（证据账本），静默轮零行=无错误证据；
日志原文快照在工作区 logs/app_r<N>.log，行号可回溯。超长日志只解析末段
（MAX_PARSE_LINES），行号仍按全文计（证据可追溯优先于解析完整）。
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 容器内被测服务进程输出重定向目标（l2._start_service 写入侧）
APP_LOG_CONTAINER_PATH = "/work/logs/app.log"
# 超长日志只解析末 N 行（服务长跑日志防内存/正则爆炸；行号按全文偏移计）
MAX_PARSE_LINES = 5000

# —— 语言分支正则（排版事实，非经验规则）——
# 每条 (pattern, level, kind)，同 line 多命中取列表序第一个（语言专属优先于通用日志级）
_EXC_CLASS = r"[A-Za-z_][\w.$]*(?:Error|Exception|Interrupt|Exit|Failure)"
_LANG_RULES: dict[str, list[tuple[re.Pattern, str, str]]] = {
    "python": [
        (re.compile(r"^Traceback \(most recent call last\):"), "error", "python_traceback"),
        (re.compile(rf"^{_EXC_CLASS}(?::\s.*)?$"), "error", "python_exception"),
        (re.compile(r"^(?:ERROR|CRITICAL|FATAL)\b.*"), "error", "error_log"),
        (re.compile(r"^WARN(?:ING)?\b.*"), "warn", "warn_log"),
    ],
    "java": [
        (re.compile(r"^Caused by: .*"), "error", "java_caused_by"),
        (re.compile(r"^\s+at [\w$.]+\("), "error", "java_stack"),
        (re.compile(rf"^{_EXC_CLASS}(?::\s.*)?$"), "error", "java_exception"),
        (re.compile(r"^(?:ERROR|SEVERE|FATAL)\b.*"), "error", "error_log"),
        (re.compile(r"^WARN(?:ING)?\b.*"), "warn", "warn_log"),
    ],
    "go": [
        (re.compile(r"^panic: .*"), "error", "go_panic"),
        (re.compile(r"^goroutine \d+ \[running\]"), "error", "go_goroutine"),
        (re.compile(r"^(?:ERROR|FATAL)\b.*"), "error", "error_log"),
        (re.compile(r"^WARN(?:ING)?\b.*"), "warn", "warn_log"),
    ],
    # node 系（javascript/typescript 运行时同为 node 形态）
    "node": [
        (re.compile(r".*\b(?:unhandledRejection|UnhandledPromiseRejection"
                    r"|uncaughtException|UncaughtException)\b.*"), "error", "node_rejection"),
        (re.compile(rf"^{_EXC_CLASS}(?::\s.*)?$"), "error", "node_error"),
        (re.compile(r"^(?:ERROR|FATAL)\b.*"), "error", "error_log"),
        (re.compile(r"^WARN(?:ING)?\b.*"), "warn", "warn_log"),
    ],
}
# 未映射语言只走通用日志级正则（诚实降级，不硬猜语言形态）
_GENERIC_RULES = [
    (re.compile(r"^(?:ERROR|CRITICAL|SEVERE|FATAL)\b.*"), "error", "error_log"),
    (re.compile(r"^WARN(?:ING)?\b.*"), "warn", "warn_log"),
]

_LANG_ALIAS = {"javascript": "node", "typescript": "node"}


def _rules_for(lang: str) -> list[tuple[re.Pattern, str, str]]:
    return _LANG_RULES.get(_LANG_ALIAS.get(lang or "", lang or ""), _GENERIC_RULES)


def parse_app_log(text: str, lang: str) -> list[dict]:
    """日志文本 → 错误/警告行条目（纯函数，零 LLM）。

    每命中行一条：{line_no(全文 1-based), level, kind, message(裁剪 300), raw_line}。
    安静日志（无排版命中）返回 []——正则宁缺毋滥，泛匹配会污染证据账本。"""
    rules = _rules_for(lang)
    lines = text.splitlines()
    offset = 0
    if len(lines) > MAX_PARSE_LINES:
        offset = len(lines) - MAX_PARSE_LINES
        lines = lines[offset:]
    out: list[dict] = []
    for i, line in enumerate(lines):
        for pat, level, kind in rules:
            if pat.match(line):
                out.append({"line_no": offset + i + 1, "level": level, "kind": kind,
                            "message": line.strip()[:300], "raw_line": line[:1000]})
                break
    return out


def choose_log_source(file_text: str, docker_text: str) -> tuple[str, str]:
    """判源（纯函数）：容器内文件优先，缺失走 docker logs 兜底，全空=empty。"""
    if file_text and file_text.strip():
        return file_text, "file"
    if docker_text and docker_text.strip():
        return docker_text, "docker_logs"
    return "", "empty"


def host_snapshot_path(workspace: str | Path, round_no: int) -> Path:
    """宿主侧快照路径：data/workspaces/<tid>/logs/app_r<N>.log（任务包规定的按轮分档落点）。"""
    return Path(workspace) / "logs" / f"app_r{round_no}.log"


def store_app_logs(conn, task_id: int, round_no: int, entries: list[dict],
                   source: str, log_path: str | None, log_lines: int) -> int:
    """条目落库（幂等）：round≤1 重跑以本轮为准清全任务（与 evaluation_rounds 同语义）；
    round>1 重存同轮先清该轮——重跑不重复计行。返回落库条数。"""
    if round_no <= 1:
        conn.execute("DELETE FROM app_logs WHERE task_id=?", (task_id,))
    else:
        conn.execute("DELETE FROM app_logs WHERE task_id=? AND round_no=?",
                     (task_id, round_no))
    n = 0
    for e in entries:
        conn.execute(
            """INSERT INTO app_logs(task_id, round_no, level, kind, line_no,
                                    message, raw_line, source, log_path, log_lines)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, round_no, e["level"], e["kind"], int(e["line_no"]),
             str(e["message"])[:300], str(e["raw_line"])[:1000],
             source, log_path, int(log_lines)))
        n += 1
    conn.commit()
    return n


def collect_and_store_app_logs(runner, container, conn, task_id: int, round_no: int,
                               workspace: str | Path, lang: str = "python") -> dict:
    """采集→宿主快照→解析→落库（图节点接线入口；失败由调用方 try/except 留痕）。

    返回 {source, host_path, log_lines, stored, error_count, warn_count,
    first_error_line}——供节点写 agent_messages 摘要。source=empty 时不落库
    （无应用进程或无输出，app_logs 零行=无错误证据的诚实缺席）。"""
    file_text, docker_text = runner.read_app_log_pair(container, APP_LOG_CONTAINER_PATH)
    text, source = choose_log_source(file_text, docker_text)
    host = host_snapshot_path(workspace, round_no)
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(text, encoding="utf-8", errors="replace")
    info = {"source": source, "host_path": str(host), "log_lines": 0, "stored": 0,
            "error_count": 0, "warn_count": 0, "first_error_line": None}
    if source == "empty":
        return info
    entries = parse_app_log(text, lang)
    n_lines = len(text.splitlines())
    info["log_lines"] = n_lines
    info["stored"] = store_app_logs(conn, task_id, round_no, entries, source=source,
                                    log_path=str(host), log_lines=n_lines)
    errs = [e for e in entries if e["level"] == "error"]
    info["error_count"] = len(errs)
    info["warn_count"] = len(entries) - len(errs)
    info["first_error_line"] = errs[0]["line_no"] if errs else None
    return info


def logs_snapshot(conn, task_id: int, round_no: int | None = None) -> dict:
    """GET /api/tasks/{tid}/app_logs 数据组装（纯 SQL 确定性，零 LLM）：
    按轮分组——概要（错误/警告计数、首错行、判源、快照路径）+ 逐条证据行。"""
    q = "SELECT * FROM app_logs WHERE task_id=?"
    args: list = [task_id]
    if round_no is not None:
        q += " AND round_no=?"
        args.append(round_no)
    rows = [dict(r) for r in conn.execute(q + " ORDER BY round_no, line_no", args)]
    by_round: dict[int, list[dict]] = {}
    for r in rows:
        by_round.setdefault(r["round_no"], []).append(r)
    rounds = []
    for rn, items in sorted(by_round.items()):
        errs = [i for i in items if i["level"] == "error"]
        rounds.append({
            "round_no": rn, "source": items[0]["source"],
            "log_path": items[0]["log_path"], "log_lines": items[0]["log_lines"],
            "error_count": len(errs), "warn_count": len(items) - len(errs),
            "first_error_line": min(i["line_no"] for i in errs) if errs else None,
            "entries": [{"line_no": i["line_no"], "level": i["level"], "kind": i["kind"],
                         "message": i["message"], "raw_line": i["raw_line"]}
                        for i in items]})
    return {"task_id": task_id, "rounds": rounds}
