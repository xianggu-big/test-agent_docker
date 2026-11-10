"""static_scan —— 语言无关静态扫描 + AI 测试设计草稿（Tier3 兜底，2026-11-01）。

动机（用户实测反馈）：非 Python 项目降级后此前直接"诚实放弃"，连静态扫描都没有——
软件初衷是"替人省配置和测试的麻烦"，不适配的语言更该给点东西。

两级产物，全部确定性代码 + 现成 LLM 管线，零新依赖：
- scan_workspace(ws)：语言无关确定性扫描——规模/结构、TODO/FIXME 风险词、
  通用风险模式（空 catch/超长函数/深嵌套）、顶层符号正则表（非 AST，语言无关）
- generate_static_drafts(...)：把 plan 拆出的测试条件展开成"输入/步骤/预期"设计草稿
  （LLM，落 test_cases framework='static_draft' source='ai'）——不执行、不伪装成
  可执行用例，报告明示"静态模式：设计已生成、未验证"。预期依据 expected_basis 照旧。
"""
import json
import logging
import re
from pathlib import Path

from ..skills.llm_client import make_chat

from .. import config
from .llm_usage import log_llm_call
from .prompt_library import SYSTEM_PROMPT

log = logging.getLogger(__name__)

# 语言无关的风险词（大小写不敏感，注释/字符串命中即计——静态模式的定位是"线索"不是"缺陷"）
RISK_WORDS = ("TODO", "FIXME", "HACK", "XXX", "WORKAROUND", "待办", "待修", "临时")

# 通用风险模式（正则，跨语言近似——静态模式只报线索不定性）
# 空 catch 的常规排版是跨行的（catch (...) 换行 {}）——逐行匹配永漏报（终验实锤：
# dotnet-notes demo 的 C# 空 catch 不命中），归入 _CROSS_LINE_PATTERNS 文件级扫描
_CROSS_LINE_PATTERNS = [
    ("空 catch（吞异常嫌疑）", re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}")),
    # except: pass 的常规排版同样是跨行的（except ...: 换行 pass）——同漏报实锤
    ("except 后直接 pass（吞异常嫌疑）", re.compile(r"except[^:]*:\s*pass\b")),
]
_RISK_PATTERNS = [
    ("裸 sleep（时序依赖嫌疑）", re.compile(r"\b(sleep\(|Thread\.sleep|time\.Sleep)\b")),
]


def _line_depth(line: str) -> int:
    """缩进深度近似（Tab=4 空格）：深嵌套是可读性/缺陷密度线索。"""
    expanded = line.expandtabs(4)
    return (len(expanded) - len(expanded.lstrip(" "))) // 4 if line.strip() else 0


# 顶层符号正则（语言 → 定义行模式）：静态模式要给 AI 一个"这里有什么可测"的清单
_SYMBOL_RES = [
    ("python", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
    ("javascript", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
    ("typescript", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
    ("java", re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\]]+\s+(\w+)\s*\(")),
    ("go", re.compile(r"^func\s+(?:\([^)]+\)\s*)?(\w+)")),
    ("c", re.compile(r"^\w[\w\s\*]*\s+\**(\w+)\s*\([^;]*\)\s*\{")),
    ("cpp", re.compile(r"^\w[\w\s\*:&<>]*\s+\**(\w+)\s*\([^;]*\)\s*\{")),
    # C#（白包阶段5）：public/private 修饰符形态与 java 同构，复用同款正则
    ("csharp", re.compile(r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\]]+\s+(\w+)\s*\(")),
    # PHP（2026-11-10 补配）：此前 .php 不在后缀白名单，PHP 项目落 static_only 后
    # 扫描 0 文件 = 与 687 同族的"静默空转"缺陷
    ("php", re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+(\w+)")),
]


def scan_workspace(ws: Path, max_funcs: int = 20) -> dict:
    """语言无关确定性扫描（纯函数，零 LLM）：给 static_only 路径一份"至少有点东西"的报告素材。"""
    ws = Path(ws)
    files = funcs = 0
    todos: list[str] = []
    long_funcs: list[dict] = []          # 超长函数（>80 行，圈复杂度近似线索）
    deep_nests: list[dict] = []          # 深嵌套（缩进 ≥6 层）
    symbols: dict[str, list[str]] = {}
    cur_lang_sym = re.compile(_SYMBOL_RES[0][1].pattern)   # 占位，逐文件按主语言选
    lang_counts: dict[str, int] = {}

    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".py", ".js", ".ts", ".jsx", ".tsx",
                                                       ".java", ".go", ".c", ".cpp", ".cc", ".h",
                                                       ".cs", ".php"):
            continue
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        files += 1
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        blob = None                        # 跨行风险模式懒加载（文件级全文）
        ext = p.suffix.lower()
        lang = {".py": "python", ".js": "javascript", ".jsx": "javascript",
                ".ts": "typescript", ".tsx": "typescript", ".java": "java",
                ".go": "go", ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".h": "c",
                ".cs": "csharp", ".php": "php"}.get(ext, "")
        lang_counts[lang] = lang_counts.get(lang, 0) + len(lines)
        sym_re = next((r for name, r in _SYMBOL_RES if name == lang), None)
        cur_func: tuple[str, int, str] | None = None
        for i, line in enumerate(lines, 1):
            for w in RISK_WORDS:
                if w in line:
                    todos.append(f"{p.relative_to(ws).as_posix()}:{i} [{w}] {line.strip()[:80]}")
                    break
            for name, rx in _RISK_PATTERNS:
                if rx.search(line):
                    todos.append(f"{p.relative_to(ws).as_posix()}:{i} [{name}] {line.strip()[:80]}")
                    break
        # 跨行风险模式（文件级，逐个定位首行——去重用 seen 集合防同形态多命中刷屏）
        if blob is None:
            blob = chr(10).join(lines)
        seen_cross = set()
        for name, rx in _CROSS_LINE_PATTERNS:
            pos = 0
            while True:
                m = rx.search(blob, pos)
                if not m:
                    break
                ln = blob.count(chr(10), 0, m.start()) + 1
                if ln not in seen_cross:
                    seen_cross.add(ln)
                    todos.append(f"{p.relative_to(ws).as_posix()}:{ln} [{name}] "
                                 f"{lines[ln - 1].strip()[:80]}")
                pos = m.end()
        for i, line in enumerate(lines, 1):
            if sym_re:
                m = sym_re.match(line)
                if m:
                    rel = p.relative_to(ws).as_posix()
                    symbols.setdefault(rel, []).append(m.group(1))
                    if cur_func:
                        span = i - cur_func[1]
                        if span > 80:
                            long_funcs.append({"file": cur_func[2], "name": cur_func[0],
                                               "lines": span})
                    cur_func = (m.group(1), i, rel)
            d = _line_depth(line)
            if d >= 24:                  # 6 层 × 4 空格
                rel = p.relative_to(ws).as_posix()
                if not deep_nests or deep_nests[-1]["file"] != rel:
                    deep_nests.append({"file": rel, "line": i, "depth": d // 4})
    primary = max(lang_counts, key=lang_counts.get) if lang_counts else ""
    return {
        "mode": "static_scan",
        "files": files,
        "primary_language": primary,
        "symbols_total": sum(len(v) for v in symbols.values()),
        "symbols": {k: v[:12] for k, v in list(symbols.items())[:15]},
        "risk_lines": todos[:40],
        "risk_count": len(todos),
        "long_functions": long_funcs[:max_funcs],
        "deep_nests": deep_nests[:10],
    }


def build_draft_prompt(requirement: str, conditions: list[dict], scan: dict) -> str:
    """静态模式草稿提示词：条件 → 测试设计要点（输入/步骤/预期），不是可执行代码。"""
    scan_text = json.dumps({k: scan[k] for k in ("files", "symbols_total", "risk_count",
                                                 "long_functions", "deep_nests")},
                           ensure_ascii=False)
    sym_rows = "\n".join(f"- {f}: {', '.join(fns[:8])}" for f, fns in
                         list(scan.get("symbols", {}).items())[:10]) or "（未提取到符号）"
    cond_rows = "\n".join(f"{i}. [{c.get('priority') or '中'}] {c['condition']}"
                          for i, c in enumerate(conditions, 1))
    return f"""## 用户需求
{(requirement or "（无额外需求，按扫描结果设计）")[:2500]}

## 静态扫描摘要（该项目无法容器执行，以下是确定性扫描结果）
{scan_text}

## 可测符号（顶层函数/方法）
{sym_rows}

## 测试条件（逐条展开设计）
{cond_rows}

## 任务（静态模式：只做测试设计，不写可执行代码）
把每条测试条件展开成一条设计草稿：input（具体输入值/场景）、steps（操作步骤，
涉及上述真实符号）、expected（明确预期结果）、expected_basis（依据：需求明示/源码推导/常识）。
语言用 {scan.get('primary_language') or '目标'} 描述步骤即可。禁止编造扫描结果之外的符号名。

## 输出格式
只输出一个 JSON 对象：
{{"drafts": [{{"condition_id": 1, "name": "test_条件要点的短名", "method": "等价类划分",
   "input": "…", "steps": "…", "expected": "…", "expected_basis": "需求明示"}}]}}"""


def parse_drafts(text: str) -> list[dict] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    drafts = data.get("drafts")
    if not isinstance(drafts, list):
        return None
    out = [d for d in drafts if isinstance(d, dict) and d.get("name") and d.get("expected")]
    return out or None


def generate_static_drafts(api_key: str, task_id: int, ws: Path, requirement: str,
                           conn) -> str:
    """static_only 路径的设计草稿生成：scan → plan 的条件 → LLM 展开 → 落库。
    framework='static_draft'：报告/评审页可见，但明确不是可执行用例（诚实呈现）。"""
    from .registry import SkillRegistry
    registry = SkillRegistry(conn)
    from .builtin import build_skills
    for s in build_skills(None).values():
        registry.register(s)
    registry.register(_static_scan_skill())
    scan = scan_workspace(ws)
    registry.invoke(task_id, _static_scan_skill(), {"workspace": str(ws)})
    conditions = [dict(r) for r in conn.execute(
        "SELECT id, condition, priority, method_hint FROM test_conditions "
        "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
    if not conditions:
        return json.dumps({"drafts": 0, "reason": "无测试条件（plan 未产出），仅完成静态扫描"},
                          ensure_ascii=False)
    llm = make_chat(phase="static_draft", api_key=api_key, temperature=0.2,
                    timeout=90, max_retries=2)
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_draft_prompt(requirement, conditions, scan))])
    log_llm_call(task_id, "static_draft", resp,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
    drafts = parse_drafts(resp.content)
    if not drafts:
        return json.dumps({"drafts": 0, "reason": "设计草稿 LLM 输出不可解析（诚实降级），静态扫描结果已落库",
                           "scan": {k: scan[k] for k in ("files", "risk_count")}}, ensure_ascii=False)
    cond_by_id = {c["id"]: c for c in conditions}
    drafts_text = json.dumps(drafts, ensure_ascii=False, indent=1)   # 草稿全文（可读存档）
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, filename, content, content_hash)
           VALUES (?, 'static_draft', 1, 'static_draft.md', ?, 'draft')""", (task_id, drafts_text))
    sid = cur.lastrowid
    for d in drafts:
        cid = d.get("condition_id")
        cond = cond_by_id.get(cid if isinstance(cid, int) else 0)
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content,
                                      priority, input, steps, expected, expected_basis)
               VALUES (?, 'ai', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, d["name"], d.get("method") or "静态设计", cond["condition"] if cond else "",
             (cond.get("priority") if cond else None) or "中",
             str(d.get("input") or ""), str(d.get("steps") or ""), str(d["expected"]),
             d.get("expected_basis") or "源码推导"))
    conn.commit()
    return json.dumps({"drafts": len(drafts), "framework": "static_draft",
                       "scan": {"files": scan["files"], "risk_count": scan["risk_count"],
                                "symbols": scan["symbols_total"]}}, ensure_ascii=False)


def _static_scan_skill():
    """静态扫描作为注册 skill（审计台账覆盖）：func 自己扫 workspace。"""
    def run(task_id: int, args: dict) -> str:
        scan = scan_workspace(Path(args["workspace"]))
        return json.dumps(scan, ensure_ascii=False)
    from .registry import Skill
    return Skill(name="static_scan", description="语言无关静态扫描（Tier3 兜底）",
                 func=run, risk_level="low", timeout_ms=30_000)
