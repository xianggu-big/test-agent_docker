"""gen_tests —— 按五种方法知识库生成用例：写入工作区 → 回灌容器 → 落库用例库。

流程：
1. 收集被测源码（排除测试文件与噪声目录，限总量防 token 爆炸）
2. 组装"五种方法骨架"提示词，调 DeepSeek 生成 JSON 用例清单
3. 解析 + 清洗（只保留 def test_ 开头的完整函数），写入 test_gen_r<N>.py
4. 重新快照进容器（下一轮 run_tests 直接可跑）
5. test_scripts + test_cases 落库（source='ai'，method=方法标签）—— 论文用例库数据源
"""
import ast
import hashlib
import json
import logging
import re
from pathlib import Path

from ..skills.llm_client import make_chat

from .. import config
from ..graph.repair import CASE_OWN_DEFECT_ERRORS
from ..store.db import get_conn
from .llm_usage import log_llm_call
from .prompt_library import (CROSS_REVIEW_SYSTEM, MAX_SOURCE_CHARS, SYSTEM_PROMPT,
                             build_cross_review_prompt, build_regen_prompt,
                             build_user_prompt)
from .test_templates import expand_template_cases

log = logging.getLogger(__name__)


def _sanitize_name(name: str, idx: int, used: set[str]) -> str:
    """把 LLM 自报的用例名规整成合法且唯一的函数名。

    A5 纪律：写文件前统一用这个名字重写 def，保证 test_cases.name 与
    pytest nodeid 一一对应——归因/定向修复（②）按 nodeid 查用例的前提。
    """
    base = re.sub(r"\W+", "_", (name or "").strip()).strip("_") or f"test_case_{idx}"
    if not base.startswith("test_"):
        base = "test_" + base
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


SOURCE_NOISE_FILES = {"setup.py", "setup.cfg", "conftest.py", "tests.py", "test.py"}
WHOLE_FILE_LIMIT = 12000  # 不超过该大小的文件整文件收录（上下文最完整；模块级词表/正则不丢）

# P1-4a（2026-10-31 体检）：生成物 import 白名单——LLM 输出的 import 原样进容器执行，
# 被测仓库提示注入可诱导 import os/subprocess/socket 等触达容器外。允许集=测试框架/
# 标准库无害子集/被测模块本身；白名单外一律剔除并留理由（确定性、零成本）。
IMPORT_WHITELIST = {
    # 测试与常用无害三方
    "pytest", "unittest", "mock", "jsonschema", "hypothesis", "freezegun",
    # 标准库无害子集（数据/文本/时间/数学——不含 os/sys/subprocess/socket/pathlib/shutil 等）
    "math", "random", "string", "textwrap", "datetime", "time", "calendar",
    "collections", "itertools", "functools", "re", "json", "decimal", "fractions",
    "statistics", "enum", "abc", "copy", "heapq", "bisect", "uuid", "base64",
    "hashlib", "typing", "dataclasses", "contextlib", "warnings", "types",
}


def _is_noise_source(p: Path) -> bool:
    """打包/配置/测试运行器文件不是被测业务代码。

    D5/D1②（2026-10-30 H1 实锤）：setup.py 曾按路径序挤占预算成为 LLM 唯一见到的"源码"，
    tests.py 曾被当被测源码收录。"""
    if p.name.lower() in SOURCE_NOISE_FILES:
        return True
    return p.name.startswith("test_") or p.name.endswith("_test.py")


JS_SOURCE_EXTS = (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
JS_NOISE_SUFFIXES = (".test.js", ".spec.js", ".test.ts", ".spec.ts", ".d.ts", ".min.js",
                     ".config.js", ".config.ts")
# jest 用例声明：test("test_xxx" / it('test_xxx'（提示词强制名字以 test_ 开头无空格）
JS_CASE_RE = re.compile(r'\b(?:test|it)\s*\(\s*[\'"]test_\w+')

# task_366 实锤：12 条用例全部写 path.join(__dirname, '..')（jest 惯例假设用例在 test/
# 子目录），但平台把用例文件直接写在仓库根 → 容器内 ROOT='/' → 全部被测脚本
# "Cannot find module" → exit 1 + stdout 空 → 12 条被误归因"疑似产品缺陷"。
# 平台布局下该形态 100% 指向仓库外，等价修正=去掉 '..' 段（LLM 本意=仓库根）；
# 只改路径脚手架，不碰断言与用例逻辑。上跳两级等更深的形态无歧义修正语义，不在此
# 处理（提示词布局纪律防其出现；真出现时 stderr 诊断段可见，见 js_adapter）。
_JS_ROOT_SELF = (
    r"path\.(?:join|resolve)\(\s*__dirname\s*,\s*(['\"])\.\.\1\s*\)")
# 后续段不含 '..'（tempered：上跳两级等更深形态语义不明，不动）且不含括号（排除嵌套调用）
_JS_ROOT_DOTDOT = (
    r"path\.(?:join|resolve)\(\s*__dirname\s*,\s*(['\"])\.\.\1\s*,\s*"
    r"((?:(?!\.\.)[^()])*?)\)")


def _fix_js_scaffold_paths(code: str) -> tuple[str, int]:
    """JS 用例路径脚手架确定性修正：path.join(__dirname, '..') → __dirname、
    path.join(__dirname, '..', x) → path.join(__dirname, x)。
    返回 (修正后代码, 修正处数)。零成本纯字符串变换，修正处由返回 JSON root_fixed 留痕。"""
    n = len(re.findall(_JS_ROOT_SELF, code))
    code = re.sub(_JS_ROOT_SELF, "__dirname", code)
    code, more = re.subn(_JS_ROOT_DOTDOT, r"path.join(__dirname, \2)", code)
    return code, n + more


def _is_js_noise(p: Path) -> bool:
    """JS 项目的非被测文件：测试/声明/压缩/配置/生成物。"""
    name = p.name.lower()
    if name.endswith(JS_NOISE_SUFFIXES) or name.startswith("test_gen_"):
        return True
    return name in ("jest.config.js", "babel.config.js", "webpack.config.js", "vite.config.js")


def _iter_source_files(ws: Path, lang: str = "python"):
    """按目标语言枚举被测源码：python=*.py；javascript/typescript=JS_SOURCE_EXTS。"""
    exts = JS_SOURCE_EXTS if lang in ("javascript", "typescript") else (".py",)
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if lang in ("javascript", "typescript"):
            if _is_js_noise(p):
                continue
        elif _is_noise_source(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.strip():
            yield p, text


def _split_top_level(text: str) -> list[tuple[str, str]]:
    """大文件按顶层 def/class 切片，模块级赋值（词表/正则等常量）并入 "(module-level)" 段。

    D1①：more.py 176KB 这类文件整收必超预算，按函数切片才能进 LLM 视野；
    常量段单独保留——titlecase 的 SMALL_WORDS 词表决定行为，丢了等于继续盲写。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines(keepends=True)

    def seg(node) -> str:
        end = getattr(node, "end_lineno", None) or node.lineno
        return "".join(lines[node.lineno - 1:end]).strip()

    chunks: list[tuple[str, str]] = []
    globals_seg: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s = seg(node)
            if s:
                chunks.append((node.name, s))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            s = seg(node)
            if s:
                globals_seg.append(s)
    if globals_seg:
        chunks.insert(0, ("(module-level)", "\n\n".join(globals_seg)))
    return chunks


def _collect_sources_detailed(ws: Path, lang: str = "python") -> tuple[list[tuple[str, str]], list[str]]:
    """被测源码收集（带跳过台账）：小文件整收、大文件按顶层符号切片、预算截断如实记录。

    JS/TS 无 Python AST：不切片、按预算顺序整收，超出的文件截断尾部并留台账（诚实呈现）。"""
    sources: list[tuple[str, str]] = []
    skipped: list[str] = []
    used = 0
    is_js = lang in ("javascript", "typescript")
    for p, text in _iter_source_files(ws, lang):
        rel = p.relative_to(ws).as_posix()
        if is_js:
            # JS：无 AST 切片。语法可粗检（花括号平衡）不拦——语法闸在容器内 node --check。
            budget_left = MAX_SOURCE_CHARS - used
            if len(text) > budget_left:
                kept = text[:budget_left]
                sources.append((rel + "（预算截断）", kept + "\n/* …（超出预算截断，完整文件见仓库）*/"))
                skipped.append(f"{rel}（{len(text)} 字符超预算，截断收录 {len(kept)}）")
                used += budget_left
            else:
                sources.append((rel, text))
                used += len(text)
            continue
        if len(text) > WHOLE_FILE_LIMIT or used + len(text) > MAX_SOURCE_CHARS:
            # 大文件或预算吃紧：按顶层 def/class 切片逐段收录（放得下多少收多少）
            placed = False
            for name, chunk in _split_top_level(text):
                key = f"{rel}::{name}"
                if used + len(chunk) > MAX_SOURCE_CHARS:
                    skipped.append(f"{key}（预算截断）")
                    continue
                sources.append((key, chunk))
                used += len(chunk)
                placed = True
            if not placed:
                skipped.append(f"{rel}（整文件 {len(text)} 字符超预算且无可容纳切片）")
            continue
        try:
            ast.parse(text)
        except SyntaxError:
            # 语法坏文件喂给 LLM 是浪费预算且必然产出废用例——如实记台账
            skipped.append(f"{rel}（语法不可解析，未收录）")
            continue
        sources.append((rel, text))
        used += len(text)
    return sources, skipped


def _collect_sources(ws: Path, lang: str = "python") -> list[tuple[str, str]]:
    """被测源码（兼容壳，repair_strategy 复写路径共用）；需要跳过台账用 _collect_sources_detailed。"""
    return _collect_sources_detailed(ws, lang)[0]


# 模型漂移第 3/4 形（2026-11-05 批4 12/12 降级实锤）：JSON 字符串值位置写 Python
# 表达式——重复 "A" * 65、拼接 "Ab1!" + "aaa"。合法 JSON 中字符串结束引号后只可能
# 是 , } ] :，出现 + / * 必然非法，因此片段级求值替换不会误伤 code 字段内嵌的
# '-' * 20（其宿主字符串的结束引号后跟的是 , 或 }，模式匹配不到它）。
_PY_STR_EXPR_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"(?:\s*[+*]\s*(?:"(?:[^"\\]|\\.)*"|\d+))*')


def _eval_str_expr(m: "re.Match") -> str:
    """字符串表达式片段求值：片段经正则约束只含字符串/数字字面量与 + *（无名称、
    无调用、无下标），eval 无注入面；求值失败保持原样交外层容错循环。"""
    try:
        val = eval(m.group(0), {"__builtins__": {}}, {})   # noqa: S307 —— 见上，无注入面
        return json.dumps(val if isinstance(val, str) else str(val))
    except Exception:                                      # noqa: BLE001
        return m.group(0)


def _loads_tolerant(s: str):
    """JSON 宽容解析：DeepSeek 模型更新后偶发在 JSON 里输出 Python 形态（2026-10-30
    D8 实锤 True/False/None 字面量；2026-11-05 批4 实锤字符串重复/拼接表达式）。
    只在解析器报错位置做字面量替换——报错点必然在字符串外，因此 code 字符串里
    的 "assert x is True" 不会被误伤；替换后重试，至多 50 次防病态输入。"""
    s = _PY_STR_EXPR_RE.sub(_eval_str_expr, s)
    for _ in range(50):
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            seg = s[e.pos:e.pos + 5]
            for lit, js in (("True", "true"), ("False", "false"), ("None", "null")):
                if seg.startswith(lit):
                    s = s[:e.pos] + js + s[e.pos + len(lit):]
                    break
            else:
                raise
    raise ValueError("tolerant parse loop limit")


def _parse_cases(text: str) -> dict | None:
    """从 LLM 回复中鲁棒提取 JSON；解析失败返回 None（诚实降级，不硬造用例）。

    用例两种合法形态（双通道）：①自由代码（code 含 def test_）②槽位模板
    （template+slots，由 test_templates 展开）——解析层两种都收，展开在后续步骤。
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = _loads_tolerant(m.group(0))   # Python 字面量宽容重试（模型 JSON 合规性漂移）
    except (json.JSONDecodeError, ValueError):
        return None
    cases = data.get("cases")
    if not isinstance(cases, list):
        return None
    # 用例代码两种语言形态：Python def test_… / jest test("test_…" 或 it("test_…"
    cleaned = [c for c in cases if isinstance(c, dict) and (
        (isinstance(c.get("code"), str) and (re.search(r"def\s+test_\w+", c["code"])
                                              or JS_CASE_RE.search(c["code"])))
        or (isinstance(c.get("template"), str) and isinstance(c.get("slots"), dict)))]
    if not cleaned:
        # 顶层槽位形态容错（D8 补跑实锤）：LLM 偶尔把整个响应用 T2 槽位格式返回
        # （顶层 target + 全 value/error 行，无标准 cases 包装）——包一层成单条
        # 模板用例交渲染器展开，不再整批判 None。
        if isinstance(data.get("target"), str) and cases and all(
                isinstance(c, dict) and ("value" in c or "error" in c) for c in cases):
            kind = "T1" if "classes" in data else "T2"
            func = data["target"].split("(", 1)[0].strip() or "case"
            data = {"imports": data.get("imports", ""),
                    "cases": [{"name": f"test_{func}_模板", "method": None,
                               "template": kind, "slots": dict(data)}]}
            return data
        return None
    data["cases"] = cleaned
    return data


def _extract_api_docs(ws: Path, max_chars: int = 2000) -> str | None:
    """C 源·docstring grounding（领域知识注入调研 §C）：AST 遍历取模块级+顶层函数/类的
    docstring，渲染为 "### 模块.符号" + 换行 + 原文。确定性、零 LLM；超预算截断时优先
    保留含 Args/Returns/Raises 段的（行为规范密度高）；无任何 docstring 返回 None。"""
    docs: list[tuple[str, str, int]] = []   # (标题, 正文, 语义密度分)
    nl = chr(10)                             # 规避 heredoc 转义坑：换行一律 chr(10)
    for p in sorted(Path(ws).rglob("*.py")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        mod = p.stem
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            docs.append((f"### {mod}（模块）{nl}{mod_doc.strip()}", mod_doc, 0))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    score = sum(k in doc for k in ("Args", "Returns", "Raises", "参数", "返回", "抛出"))
                    docs.append((f"### {mod}.{node.name}{nl}{doc.strip()}", doc, score))
    if not docs:
        return None
    # 超预算：先按语义密度降序保优质，再按标题序稳定输出
    docs.sort(key=lambda d: (-d[2], d[0]))
    kept, used = [], 0
    for title, _body, _score in sorted(docs, key=lambda d: (-d[2], d[0])):
        if used + len(title) > max_chars:
            continue
        kept.append(title)
        used += len(title)
    return nl.join(kept) if kept else None


def _extract_usage_examples(ws: Path, limit: int = 5, max_chars: int = 1600) -> str | None:
    """A 源·repo 校准（领域知识注入调研 §A，防迁就是红线）：扫仓库自带 tests/ 的**调用形态**——
    import 被测包的语句 + 测试函数内的调用表达式 + pytest.raises 的异常触发方式，**剥除断言行**。
    只示范"怎么正确调用"（参数形态/import 方式/异常触发），不是预期值来源——
    预期值由需求/docstring 决定；对齐自带测试的预期=对齐实现现状，会加剧 2412.14137 迁就。
    生成时无法预知哪些自带测试会过（要先跑一遍，成本翻倍）→ 实现口径=全部自带测试的
    调用形态+剥断言（复审修正①），剥了断言后过不过已无关紧要。无自带测试返回 None。"""
    table = _module_top_level(ws)
    nl = chr(10)
    snippets: list[str] = []
    for p in sorted(Path(ws).rglob("*.py")):
        if len(snippets) >= limit:
            break
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        # 只看仓库自带测试（被测源码已由 _collect_sources 收录，这里要的是"别人怎么调它"）；
        # Agent/人工产物（test_gen_*/test_imported/test_acceptance）不是自带锚点，排除
        if not (p.name.startswith("test_") or p.name.endswith("_test.py")):
            continue
        if p.name.startswith(("test_gen_", "test_imported", "test_acceptance")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()

        def seg(node) -> str:
            return (lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else "").strip()

        kept: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) \
                    else (node.names[0].name.split(".")[0] if node.names else "")
                if mod in table:      # 只留 import 被测包的语句（pytest/hypothesis 不是锚点）
                    kept.append(seg(node))
            elif isinstance(node, ast.FunctionDef):
                # 下钻测试函数体：调用表达式语句 + raises 上下文（异常触发方式）；
                # assert 语句天然不收（不是 Expr/With）——剥断言是防迁就切分线
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call):
                        kept.append(seg(sub))
                    elif isinstance(sub, ast.With):
                        for item in sub.items:
                            ctx = item.context_expr
                            if isinstance(ctx, ast.Call):
                                fn = ctx.func
                                name = fn.attr if isinstance(fn, ast.Attribute) \
                                    else (fn.id if isinstance(fn, ast.Name) else "")
                                if name == "raises":
                                    kept.append(seg(sub))
            if len(kept) >= 8:
                break
        kept = [s for s in kept if s and not s.startswith("assert")][:8]
        if kept:
            snippets.append(f"# {p.relative_to(ws).as_posix()}" + nl + nl.join(kept))
    text = (f"{nl}{nl}".join(snippets))[:max_chars] if snippets else None
    # kill 检查（防自欺，调研文档§每源一 kill）：残片必须不含断言——剥除断言是防迁就切分线
    if text and re.search(r"(?m)^\s*assert\b", text):
        cleaned = nl.join(l for l in text.split(nl) if not re.match(r"\s*assert\b", l))
        text = cleaned[:max_chars]
    return text or None


def _module_top_level(ws: Path) -> dict[str, set[str]]:
    """被测工作区各模块的顶层符号表（静态闸用）：module stem -> {def/class 名}。"""
    table: dict[str, set[str]] = {}
    for p in ws.rglob("*.py"):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        # 包入口以包目录名为 import 名（D3：否则 from pkg import x 的闸校验形同虚设）
        key = p.parent.name if p.stem == "__init__" else p.stem
        # Loop-10（三角色实验首批实锤）：re-export 型包的 __init__.py 全是
        # `from .sub import X` 转发（tenacity/requests 均如此），ast 里不是
        # FunctionDef——把转发名也纳入符号表，否则静态闸误杀合法 import
        if p.stem == "__init__":
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
                elif isinstance(node, ast.Assign):
                    pass  # __all__ 等赋值不展开（v2：解析 __all__ 更精确）
        table.setdefault(key, set()).update(names)
    return table


def _decorator_is_parametrize(dec: ast.expr) -> bool:
    """N1（防御矩阵 v2）：参数化装饰器判定——pytest.mark.parametrize / parameterized.expand
    及直接以 parametrize/parameterized 命名的装饰器均命中。带参装饰器是 Call 节点，先解包。"""
    if isinstance(dec, ast.Call):
        dec = dec.func
    names: list[str] = []
    node: ast.expr = dec
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return any(n in ("parametrize", "parameterized", "parameterized_class") for n in names)


def _patch_target_hits(tree: ast.AST, table: dict) -> list[str]:
    """N2（防御矩阵 v2）：mock 目标解析进「被测模块」的调用点清单。

    覆盖四形态：mock.patch("mod.f") 字符串 / patch.object(mod, "f") 对象 /
    monkeypatch.setattr(mod, "f", …) 对象 / monkeypatch.setattr("mod.f", …) 字符串。
    只有目标首段命中工作区被测模块名（table 键）才判命中——mock 外部依赖
    （requests/网络/时钟/文件系统）放行不误杀。"""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        names: list[str] = []
        while isinstance(f, ast.Attribute):
            names.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            names.append(f.id)
        base = names[0] if names else ""
        if not (base in ("patch", "setattr")
                or (base == "object" and "patch" in names[1:])):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        target = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            target = arg.value.split(".")[0]
        elif isinstance(arg, ast.Name):
            target = arg.id
        if target and target in table:
            hits.append(ast.unparse(node.func))
    return hits


# B2（任务卡-369 20261105）SUT 功能函数候选清单（最小版）：
# Python 用 ast 顶层函数；JS 用正则（核查时已验证可提 60+ 名）；
# 0 函数文件显式标注「纯数据文件，禁作被测单元」（369 questions.js 形态）
_JS_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:function\s+([A-Za-z_$][\w$]*)\s*\("
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\s*)?\()"
    r"|(?:^|\n)\s*([A-Za-z_$][\w$]*)\s*:\s*function\s*\(")


def _sut_manifest(sources: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for fname, code in sources:
        if fname.startswith("test_") or fname.endswith(("_test.py", ".test.js", "_test.js")):
            continue                                 # 剔除测试生成物/自带测试（jest testMatch 同口径）
        funcs: list[str] = []
        if fname.endswith(".py"):
            try:
                tree = ast.parse(code)
            except SyntaxError:
                tree = None
            if tree is not None:
                funcs = [n.name for n in tree.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            for m in _JS_FUNC_RE.finditer(code or ""):
                name = next(g for g in m.groups() if g)
                if name and name not in funcs:
                    funcs.append(name)
        if funcs:
            out.append(f"{fname}: {', '.join(funcs[:12])}"
                       + ("…" if len(funcs) > 12 else ""))
        else:
            out.append(f"{fname}（纯数据文件，无可执行函数——禁作被测单元）")
    return out


# B1（任务卡-369 20261105）JS 自导自演文本启发式（无 AST，DEFENSE_V2_ENABLED 开才生效；
# 369 实锤形态：对全大写导出数据数组重新赋值/重组 QUESTIONS = QUESTIONS.filter(...)）。
# 诚实边界：启发式有误杀/漏杀可能，拦截率由 F4 红队格度量（论文如实标注）。
_JS_REDECL_CONST_RE = re.compile(
    r"^\s*(?:const|let|var)\s+[A-Z][A-Z_0-9]{2,}\s*=\s*[^=]", re.M)
_JS_DATA_RECHAIN_RE = re.compile(
    r"^\s*[A-Z][A-Z_0-9]{2,}\s*=\s*[A-Z][A-Z_0-9]*\s*\.\s*"
    r"(?:filter|map|concat|slice|sort|reverse|splice|push|pop|shift|unshift)\s*\(", re.M)
# 369 实锤主形态（4/4 命中）：vm.runInContext("QUESTIONS = QUESTIONS.filter(...)", ctx)
# ——对导出数据的重新赋值嵌在 vm 脚本字符串里（只读如 QUESTIONS.length 不命中）
_JS_VM_REASSIGN_RE = re.compile(
    r"runInContext\s*\(\s*[\"']\s*[A-Z][A-Z_0-9]{2,}\s*=[^=]")


def _js_selfplay_filter(cases: list[dict]) -> tuple[list[dict], list[str]]:
    """JS 自导自演检测（文本启发式）：①顶层重声明全大写导出常量 ②对全大写标识符
    挂数据重组链（filter/map/concat/...）——两种形态都是"用例自己改数据自己断言"
    （被测功能零执行，369 的 4 条删除用例同型）。正常调用型用例（函数调用/断言
    返回值）不受影响。"""
    kept, rejected = [], []
    for c in cases:
        code = c.get("code") or ""
        why = None
        if _JS_REDECL_CONST_RE.search(code):
            why = "顶层重声明全大写导出常量（疑似重组被测数据而非调用被测功能）"
        elif _JS_DATA_RECHAIN_RE.search(code):
            why = "对全大写导出标识符挂数据重组链（自导自演：被测功能未执行）"
        elif _JS_VM_REASSIGN_RE.search(code):
            why = "vm 脚本内对全大写导出标识符重新赋值（369 实锤形态：改 const 数据" \
                  "必然 TypeError 且被测功能未执行）"
        if why:
            rejected.append(f"{c.get('name')}: {why}")
        else:
            kept.append(c)
    return kept, rejected


def _static_filter(cases: list[dict], ws: Path) -> tuple[list[dict], list[str]]:
    """生成后静态闸（§3.6 第 1 道，确定性、零成本）：
    语法可解析 + from-import 的符号在被测模块顶层存在。剔除理由如实返回。
    JS/TS 分支：Python AST 不适用——旗标关时跳过静态闸（JS 的安全闸=容器内
    node --check 语法检查，在收集闸步骤执行），理由如实标注；
    DEFENSE_V2_ENABLED 开=B1 文本启发式闸（JS 自导自演检测，任务卡-369 B1）。"""
    if cases and any(JS_CASE_RE.search((c.get("code") or "")) for c in cases):
        if config.DEFENSE_V2_ENABLED:
            return _js_selfplay_filter(cases)
        return cases, []   # JS 形态：闸移交收集步骤（node --check），这里不做 AST 审计
    table = _module_top_level(ws)
    kept, rejected = [], []
    for c in cases:
        try:
            tree = ast.parse(c["code"])
        except SyntaxError as e:
            rejected.append(f"{c.get('name')}: 语法错误({e.lineno}行)")
            continue
        ok = True
        for node in ast.walk(tree):
            # P1-4a：import 白名单审计（Import 与 ImportFrom 都查）——提示注入防线，
            # 白名单外（os/subprocess/socket/requests...）整案剔除并留理由
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod not in IMPORT_WHITELIST and mod not in table:
                        rejected.append(f"{c.get('name')}: import {alias.name} 不在白名单"
                                        f"（测试框架/标准库无害子集/被测模块之外不执行）")
                        ok = False
                        break
            if not ok:
                break
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            mod = node.module.split(".")[0]
            if mod not in IMPORT_WHITELIST and mod not in table:
                rejected.append(f"{c.get('name')}: from {node.module} import … 不在白名单"
                                f"（测试框架/标准库无害子集/被测模块之外不执行）")
                ok = False
                break
            if mod in table:
                for alias in node.names:
                    if alias.name != "*" and alias.name not in table[mod]:
                        rejected.append(f"{c.get('name')}: from {mod} import {alias.name} 不存在")
                        ok = False
                        break
            if not ok:
                break
        # N1（防御矩阵 v2）：参数化装饰器整案剔除（闸层兜底，提示词第 6 条的确定性对偶）
        if ok and config.DEFENSE_V2_ENABLED:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(_decorator_is_parametrize(d) for d in node.decorator_list):
                        rejected.append(f"{c.get('name')}: 参数化装饰器（单条用例=单个测试函数，"
                                        f"parametrize 会被整案剔除）")
                        ok = False
                        break
        # N2（防御矩阵 v2）：mock 被测模块整案剔除（mock 被测单元=掩盖真实行为，无证伪能力；
        # mock 外部依赖放行）
        if ok and config.DEFENSE_V2_ENABLED:
            hits = _patch_target_hits(tree, table)
            if hits:
                rejected.append(f"{c.get('name')}: mock 被测模块（{'/'.join(hits[:2])}——"
                                f"仅允许 mock 网络/文件系统/时钟等外部依赖）")
                ok = False
        if ok:
            kept.append(c)
    return kept, rejected


def _audit_one_assert(test: ast.expr) -> tuple[bool, str | None]:
    """单条断言的强弱判定：(是否强, 弱原因)。强 = 具体值/关系 oracle。

    BoolOp 联接（assert a==1 and b==2）逐臂审计：全部臂强才判强——
    整体落"裸真值"会把两臂皆具体比较的好断言误杀（module_check FINDING）。
    """
    if isinstance(test, ast.BoolOp):
        for i, arm in enumerate(test.values, 1):
            ok, why = _audit_one_assert(arm)      # 递归：嵌套联接同样逐臂
            if not ok:
                return False, f"联接第{i}臂弱({why})"
        return True, None
    if isinstance(test, ast.Compare):
        comps = [test.left] + list(test.comparators)
        has_rel = any(not isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops)
        if has_rel:
            return True, None                       # ==/!=/</>/in 等值或序关系
        none_check = any(isinstance(c, ast.Constant) and c.value is None for c in comps)
        bool_check = any(isinstance(c, ast.Constant) and isinstance(c.value, bool)
                         for c in comps)
        if bool_check:
            return True, None                       # is True/False：显式布尔 oracle
        if none_check:
            return False, "is (not) None 无具体预期值"
        return True, None
    if isinstance(test, ast.Call):
        fn = test.func
        name = fn.attr if isinstance(fn, ast.Attribute) else \
            (fn.id if isinstance(fn, ast.Name) else "")
        if name == "raises":
            return True, None                       # 异常类型 oracle
        return False, f"裸调用真值断言({name or 'expr'})"
    return False, "裸真值/常量断言"


def assertion_audit(code: str) -> dict:
    """断言质量审计（纯函数，方案A 的度量与闸共用）：统计用例内强/弱断言。

    强 = 具体关系比较（==/!=/序关系/in）、is True/False、pytest.raises(异常类型)、
    联接断言逐臂皆强（assert a==1 and b==2）；
    弱 = 裸真值断言、is (not) None、isinstance、无断言、联接断言含弱臂 —— 不具备证伪具体预期值的能力
    （动机：2412.14137 实证生成器会写"为通过而设计"的断言；UW ASE'25 oracle 实证）。
    返回 {total, strong, weak, reasons}；无断言整案计弱。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["语法不可解析"]}
    strong, weak, reasons = 0, 0, []
    # raises 上下文管理器（with pytest.raises(...)）也是显式 oracle，与 assert 同权统计
    raises_calls = []
    for w in ast.walk(tree):
        if isinstance(w, ast.With):
            for item in w.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else \
                        (fn.id if isinstance(fn, ast.Name) else "")
                    if name == "raises":
                        raises_calls.append(ctx)
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    if not asserts and not raises_calls:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["无断言（无 oracle）"]}
    for a in asserts:
        ok, why = _audit_one_assert(a.test)
        if ok:
            strong += 1
        else:
            weak += 1
            reasons.append(why)
    strong += len(raises_calls)
    return {"total": strong + weak, "strong": strong, "weak": weak, "reasons": reasons}


def _verify_expected_basis(cases: list[dict], requirement: str) -> list[str]:
    """D8（2026-10-31 真机实锤）：LLM 会把推断预期洗成「需求明示」（expected_basis 假标注）——
    归因语义信任该字段，假标注即假 product_defect（titlecase 任务160：需求未提「AND 转小写」
    却标注需求明示）。确定性校验：预期文本与需求 2-gram 零重叠的「需求明示」降级「模型推断」。
    诚实边界：需求提及主题但规则理解错（缩写词规则优先于小词规则）无法用重叠判定，
    仍靠执行暴露——oracle 问题本质，论文讨论素材。只校验 code 形态用例（模板槽位的
    expected_basis 由渲染器语义负责）。"""
    from .evaluate import _grams
    req = _grams(requirement or "")
    downgraded: list[str] = []
    for c in cases:
        if c.get("template"):
            continue
        if (c.get("expected_basis") or "").strip() == "需求明示":
            eg = _grams((c.get("expected") or "") + (c.get("input") or ""))
            if req and eg and not (eg & req):
                c["expected_basis"] = "模型推断"
                downgraded.append(c.get("name") or "?")
    return downgraded


def _condition_gaps(conditions: list[dict], cases: list[dict]) -> list[dict]:
    """方案C：确定性 2-gram 映射（与 map_requirements 启发式同源）找出无用例对准的条件。"""
    from .evaluate import _heuristic_map          # 延迟导入避免环形依赖
    pseudo = [{"id": i + 1, "condition": c["condition"]} for i, c in enumerate(conditions)]
    rows = [{"name": c["name"], "expected": c.get("expected") or "",
             "content": c.get("content") or c.get("code") or ""} for c in cases]
    mapping = _heuristic_map(pseudo, rows)
    return [c for c, p in zip(conditions, pseudo) if not mapping.get(p["id"])]


def _rebuild_imported_file(conn, tid: int, ws: Path) -> str:
    """以库为准重建人工/AI 插入用例文件（仅 enabled=1），随下次 provision 快照进容器。
    文件名按项目语言分流（晚间遗留实锤：JS 项目写 test_imported.py jest 不收集）——
    JS → test_imported.test.js（jest testMatch 收集 + // 注释头）；其余 → test_imported.py。"""
    imported = conn.execute(
        """SELECT c.content FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.source='imported' AND c.enabled=1 ORDER BY c.id""", (tid,)).fetchall()
    lang_row = None
    try:
        lang_row = conn.execute(
            """SELECT p.primary_language FROM test_tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.id=?""", (tid,)).fetchone()
    except Exception:  # noqa: BLE001 —— 轻量测试环境可能无 tasks/projects 表：默认 python 命名
        pass
    is_js = bool(lang_row) and lang_row["primary_language"] == "javascript"
    fname = "test_imported.test.js" if is_js else "test_imported.py"
    header = ("// 人工/AI 插入用例（test_cases.source=imported，enabled=1）" if is_js
              else '"""人工/AI 插入用例（test_cases.source=imported，enabled=1）"""')
    content = header + "\n\n" + "\n\n".join(r["content"].strip() for r in imported) + "\n"
    # 清掉另一种语言的旧文件（语言判定不随任务重跑变化，防御性清理残留）
    for stale in ("test_imported.py", "test_imported.test.js"):
        if stale != fname:
            (Path(ws) / stale).unlink(missing_ok=True)
    (Path(ws) / fname).write_text(content, encoding="utf-8")
    return content


def prune_failing_cases(ws: Path, pairs: list[tuple[str, str]],
                        task_id: int, conn) -> dict:
    """②定向修复（TestGen-LLM 准入思想）：上一轮失败用例按归因分流——

    - 用例自身坏掉的强信号（ImportError/SyntaxError/AttributeError 等）→ 剪除：
      生成文件走 AST 摘除函数，imported 文件以库为准重建（enabled=0 保留审计记录）；
    - AssertionError（疑似产品缺陷）→ 一律保留为证人（§7.2 防造假铁律）；
    - 仓库自带测试永远不动（那是被测项目的一部分，不是 Agent 的产物）。
    """
    pruned, kept = [], []
    for nodeid, err in pairs:
        fname, _, func = nodeid.partition("::")
        agent_file = fname.startswith(("test_gen_r", "test_imported"))
        if not agent_file or err not in CASE_OWN_DEFECT_ERRORS:
            kept.append({"nodeid": nodeid, "err": err or "unknown"})
            continue
        # DB 记录保留但停用（论文审计台账），文件层面真正摘除
        conn.execute(
            """UPDATE test_cases SET enabled=0 WHERE name=? AND enabled=1 AND script_id IN
               (SELECT id FROM test_scripts WHERE task_id=?)""", (func, task_id))
        conn.commit()
        path = Path(ws) / fname
        if fname == "test_imported.py":
            _rebuild_imported_file(conn, task_id, ws)
        elif path.exists():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                tree.body = [n for n in tree.body
                             if not (isinstance(n, ast.FunctionDef) and n.name == func)]
                path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
            except SyntaxError:
                path.unlink(missing_ok=True)  # 整文件已不可解析，整体废案
        pruned.append({"nodeid": nodeid, "err": err})
    return {"pruned": pruned, "kept": kept}


def refill_condition_gaps(llm, sources: list[tuple[str, str]], requirement: str,
                          conditions: list[dict], cases: list[dict], ws: Path,
                          used_names: set[str], include_methods: bool, pbt: bool,
                          task_id: int | None = None
                          ) -> tuple[list[dict], str, dict]:
    """方案C 核心（llm 注入可单测）：查条件缺口 → 定向补生成恰 1 次 → 返回 (新用例, imports, 信息)。"""
    gaps = _condition_gaps(conditions, cases)
    info: dict = {"uncovered_before": len(gaps), "refill_generated": 0,
                  "uncovered_after": len(gaps)}
    if not gaps:
        return [], "", info
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_user_prompt(
                           sources, requirement, None, None, gaps,
                           include_methods=include_methods, pbt=pbt, cond_forced=True,
                           templates=False,   # 补生成轮保持自由通道（定向补缺不做结构先验）
                           existing_cases=[c["name"] for c in cases]))])
    log_llm_call(task_id, "refill", resp)
    data = _parse_cases(resp.content)
    if not data:
        info["note"] = "补生成轮 LLM 输出不可解析（诚实放弃）"
        return [], "", info
    data["cases"], rejected = _static_filter(data["cases"], ws)
    info["static_rejected"] = rejected
    out = []
    root_fixed = 0
    for c in data["cases"]:
        def_name = _sanitize_name(c.get("name"), len(used_names), used_names)
        code = c["code"].strip()
        code, n_fixed = _fix_js_scaffold_paths(code)   # JS 脚手架修正（Python 代码无此形态=无操作）
        root_fixed += n_fixed
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name
        out.append(c)
    if root_fixed:
        info["root_fixed"] = root_fixed
    info["refill_generated"] = len(out)
    info["uncovered_after"] = len(_condition_gaps(conditions, cases + out))
    return out, (data.get("imports") or "").strip(), info


def apply_assertion_gate(cases: list[dict], gate: bool = False
                         ) -> tuple[list[dict], list[str], dict]:
    """方案A 核心（纯函数）：断言质量审计恒算；gate=True 时剔除全弱断言用例（无证伪能力）。
    返回 (保留用例, 剔除理由列表, 统计)。混合断言（含 ≥1 强）的用例保留。"""
    audits = [assertion_audit(c["code"]) for c in cases]
    stats = {"cases": len(audits),
             "weak_cases": sum(1 for a in audits if a["strong"] == 0),
             "assert_total": sum(a["total"] for a in audits),
             "assert_weak": sum(a["weak"] for a in audits)}
    rejected: list[str] = []
    if not gate:
        return cases, rejected, stats
    kept = []
    for c, a in zip(cases, audits):
        if a["strong"] == 0:
            rejected.append(f"{c.get('name')}: {a['reasons'][0]}")
        else:
            kept.append(c)
    stats["gate_rejected"] = len(rejected)
    return kept, rejected, stats


def parse_cross_review(text: str) -> dict[str, dict] | None:
    """从审查 LLM 回复鲁棒提取 {name: {verdict, reason}}；不可用返回 None（fail-open）。

    只认 verdict∈{pass,fail} 且 name 为字符串的条目；一条有效判决都没有 = 没审，
    同样返回 None 交由调用方放行（诚实降级，不硬造判决）。
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        return None
    out: dict[str, dict] = {}
    for r in reviews:
        if (isinstance(r, dict) and isinstance(r.get("name"), str)
                and r.get("verdict") in ("pass", "fail")):
            out[r["name"]] = {"verdict": r["verdict"],
                              "reason": str(r.get("reason") or "")[:200]}
    return out or None


def cross_review_gate(llm, cases: list[dict], requirement: str,
                      sources: list[tuple[str, str]], ws: Path,
                      used_names: set[str], task_id: int | None = None
                      ) -> tuple[list[dict], dict]:
    """§3.6 第 3 道闸：LLM 交叉语义审（V9，审查者角色，≠生成器自审走过场）。

    逐案审断言是否对应需求；fail 用例**单独重造 1 次**（受控，不无限循环）→ 重造产物
    复审仍 fail 则剔除（单案处置不拖累整批——这正是与"整批废案"的可收集闸的区别）。
    - 审查输出不可解析 → fail-open 全放行（辅助闸不因自身故障废掉整批生成，
      与 review_report 失败放行同语义）；复审不可解析同样放行。
    - 无需求文本 → 跳过（没有对齐基准，无从审起）。
    返回 (用例清单, 台账信息)；LLM 调用逐次记 llm_calls（phase=cross_review/regen_case）。
    """
    info: dict = {"reviewed": len(cases), "failed": [], "regenerated": 0,
                  "dropped": [], "note": ""}
    if not cases or not (requirement or "").strip():
        info["note"] = "无用例或无需求文本，跳过交叉审（无对齐基准）"
        return cases, info
    resp = llm.invoke([("system", CROSS_REVIEW_SYSTEM),
                       ("user", build_cross_review_prompt(requirement, cases))])
    log_llm_call(task_id, "cross_review", resp)
    verdicts = parse_cross_review(resp.content)
    if verdicts is None:
        info["note"] = "审查输出不可解析（fail-open 放行，留痕备查）"
        return cases, info
    failed = [c for c in cases if verdicts.get(c["name"], {}).get("verdict") == "fail"]
    info["failed"] = [{"name": c["name"], "reason": verdicts[c["name"]]["reason"]}
                      for c in failed]
    kept = [c for c in cases if verdicts.get(c["name"], {}).get("verdict") != "fail"]
    if not failed:
        return cases, info
    # 单独重造：逐案定向重造（原案被否决的理由随提示词进入），产物过静态闸 + def 名对齐
    regen: list[dict] = []
    for i, c in enumerate(failed):
        r2 = llm.invoke([("system", SYSTEM_PROMPT),
                         ("user", build_regen_prompt(sources, requirement, c,
                                                     verdicts[c["name"]]["reason"]))])
        log_llm_call(task_id, "regen_case", r2)
        data = _parse_cases(r2.content)
        new: dict | None = None
        if data and data["cases"]:
            cand, _ = _static_filter(data["cases"], ws)
            if cand:
                one = cand[0]                     # 单案重造只取第一条
                def_name = _sanitize_name(one.get("name"), len(used_names), used_names)
                code = one["code"].strip()
                m = re.search(r"def\s+\w+", code)
                if m:
                    code = code[:m.start()] + f"def {def_name}" + code[m.end():]
                one["code"], one["name"] = code, def_name
                new = one
        if new:
            regen.append(new)
            info["regenerated"] += 1
            info["failed"][i]["regen_as"] = new["name"]   # 台账可追溯：原案 → 重造案名
        else:
            info["dropped"].append({"name": c["name"], "reason": "重造失败（输出不可用或未过静态闸）"})
    # 复审（受控 1 次）：仍 fail 剔除——迁就性断言宁缺毋滥；不可解析 fail-open 放行
    if regen:
        resp3 = llm.invoke([("system", CROSS_REVIEW_SYSTEM),
                            ("user", build_cross_review_prompt(requirement, regen))])
        log_llm_call(task_id, "cross_review", resp3)
        v3 = parse_cross_review(resp3.content)
        survived = []
        for c in regen:
            if v3 is not None and v3.get(c["name"], {}).get("verdict") == "fail":
                info["dropped"].append({"name": c["name"], "reason": v3[c["name"]]["reason"]})
            else:
                survived.append(c)
        regen = survived
    out = kept + regen
    info["passed_cases"] = len(out)
    return out, info


SEED_SYSTEM = (
    "你是测试工程师。生成 1 条最简 pytest 用例（结构样板）：单测试函数、单断言、"
    "覆盖需求中最简单的一条行为。禁止参数化装饰器、fixture、辅助函数。"
    '只输出 JSON：{"imports": "...", "code": "def test_xxx():\\n    ..."}。'
)


def _generate_seed(llm, ws: Path, requirement: str, runner, container, fname: str,
                   tries: int = 3):
    """种子自举阶段（docs/种子自举模板设计-20261105）：生成 1 条最简用例 → 容器 collect
    验证 → 通过则返回种子全文（作为批量生成的结构锚点）。断言失败不算模板失败
    （collect 过=结构成立）；收集失败带原因重试 ≤tries 次；仍失败 → None（诚实降级，
    批量生成不带锚点照常进行）。种子文件验证后删除（结构已提取，不留执行干扰）。"""
    for attempt in range(1, tries + 1):
        try:
            src_pairs, _ = _collect_sources_detailed(ws, "python")
            src_text = "\n".join(f"### {p}\n```python\n{c}\n```" for p, c in src_pairs)
            resp = llm.invoke([("system", SEED_SYSTEM),
                               ("user", f"## 需求\n{requirement or '（按源码行为设计）'}\n\n"
                                        f"## 被测源码\n{src_text or '（无）'}")])
        except Exception as e:  # noqa: BLE001
            log.warning("种子生成 attempt=%s LLM 失败: %s", attempt, e)
            continue
        # 种子格式是单层 {imports, code}，不走 _parse_cases（它只认 cases 数组形态）
        m = re.search(r"\{.*\}", resp.content, re.S)
        if not m:
            log.warning("种子生成 attempt=%s 无 JSON", attempt)
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            try:
                data = _loads_tolerant(m.group(0))   # 模型漂移字面量容错（同批量通道）
            except (json.JSONDecodeError, ValueError):
                log.warning("种子生成 attempt=%s 输出不可解析", attempt)
                continue
        code = (data.get("code") or "").strip()
        imports = (data.get("imports") or "").strip()
        if not code or "def test_" not in code:
            log.warning("种子生成 attempt=%s 缺有效 code", attempt)
            continue
        content = ('"""结构种子（seed bootstrap，验证后删除）"""\n'
                   + (imports + "\n\n" if imports else "") + code + "\n")
        (ws / fname).write_text(content, encoding="utf-8")
        try:
            runner.copy_dir_into(container, ws)   # 种子回灌容器（collect 在容器内执行）
        except Exception as e:  # noqa: BLE001
            log.warning("种子回灌失败: %s", e)
            continue
        r = runner.exec_in(container, ["pytest", "--collect-only", "-q", fname],
                           timeout_s=120)
        if r.exit_code == 0:
            (ws / fname).unlink(missing_ok=True)   # 结构已提取，删除防执行干扰
            return content
        log.warning("种子 collect 失败 attempt=%s: %s", attempt, (r.stdout + r.stderr)[-200:])
        (ws / fname).unlink(missing_ok=True)
    return None


def generate_tests(api_key: str, args: dict, task_id: int, runner) -> str:
    """生成/增补共用管线（三道闸同源保证）：
    - 常规轮：args.round_no → test_gen_r{N}.py（version=N）
    - 对话增补轮（两阶段评审/AI 代审 regenerate）：args.feedback + args.fb_no →
      test_gen_fb{N}.py（version=调用方给定，独立文件不动已批准用例），phase=gen_feedback
    """
    ws = Path(args["workspace"])
    feedback = (args.get("feedback") or "").strip()
    fb_no = args.get("fb_no")
    phase = "gen_feedback" if feedback else "gen_tests"
    lang = (args.get("module_hint") or "python").lower()
    is_js = lang == "javascript"          # JS 执行链路 Tier2：jest 形态生成 + node --check 闸
    sources, source_skipped = _collect_sources_detailed(ws, lang)
    if source_skipped:
        # 诚实呈现能力边界：哪些代码没进 LLM 视野（旧版静默丢弃是 H1 假缺陷根因之一）
        log.warning("gen_tests task=%s 源码预算截断 %d 段: %s",
                    task_id, len(source_skipped), "; ".join(source_skipped)[:300])
    if not sources:
        return json.dumps({"generated": 0, "reason": "未发现可测源码",
                           "source_skipped": source_skipped}, ensure_ascii=False)

    conn = get_conn()
    conditions = [dict(r) for r in conn.execute(
        "SELECT condition, priority, method_hint FROM test_conditions "
        "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()] or None
    # 条件超限降采样（E2E 三跑实锤：43 条条件→40+ 用例 JSON 超 DeepSeek 输出上限截断不可解析，
    # 20 条则稳定成功）：按优先级 高>中>低 保 15 条，其余由条件缺口回灌/评审增补接力——
    # 长度爆炸在源头治，不指望解析器救截断。缺口条数如实留痕（摘要可查）。
    conditions_truncated = 0
    if conditions and len(conditions) > 15:
        prio = {"高": 0, "中": 1, "低": 2}
        conditions.sort(key=lambda c: prio.get((c.get("priority") or "中"), 1))
        conditions_truncated = len(conditions) - 15
        conditions = conditions[:15]
        log.warning("gen_tests task=%s 条件 %d 条超限，按优先级保 15（截 %d）",
                    task_id, conditions_truncated + 15, conditions_truncated)

    # —— 三源 grounding 注入（各自独立开关，默认关=提示词逐字节不变） ——
    api_docs = _extract_api_docs(ws) if config.DOCSTRING_GROUNDING_ENABLED else None
    usage_examples = _extract_usage_examples(ws) if config.USAGE_EXAMPLES_ENABLED else None

    llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL,
                     temperature=0.2, timeout=90, max_retries=2)
    seed_code = None
    if (config.SEED_BOOTSTRAP_ENABLED and not feedback and lang == "python"
            and runner is not None and args.get("container_name")):
        try:
            container = runner.client.containers.get(args["container_name"])
            seed_code = _generate_seed(llm, ws, args.get("requirement", ""), runner,
                                       container, "test_seed_probe.py")
            if seed_code:
                log.info("gen_tests task=%s 种子自举成功（结构锚点就位）", task_id)
        except Exception as e:  # noqa: BLE001 —— 种子失败 fail-open，批量照常
            log.warning("种子自举失败（fail-open）: %s", e)

    sut_manifest = (_sut_manifest(sources)
                    if config.SUT_MANIFEST_ENABLED and not feedback else None)
    if sut_manifest:
        log.info("gen_tests task=%s SUT 清单注入 %d 项（B2）", task_id, len(sut_manifest))
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_user_prompt(sources, args.get("requirement", ""),
                                                  args.get("coverage_gaps"),
                                                  args.get("coverage_target"),
                                                  conditions,
                                                  include_methods=config.GEN_METHODS_ENABLED,
                                                  boundary_attack=config.GEN_BOUNDARY_ATTACK,
                                                  pbt=config.PBT_ENABLED,
                                                  seed_example=seed_code,
                                                  templates=config.TEMPLATES_ENABLED,
                                                  state_transition=config.STATE_TRANSITION_ENABLED,
                                                  metamorphic=config.METAMORPHIC_ENABLED,
                                                  api_docs=api_docs,
                                                  usage_examples=usage_examples,
                                                  feedback_text=feedback or None,
                                                  pitfalls=config.PITFALL_KB_ENABLED,
                                                  module_lang=args.get("module_hint", ""),
                                                  target_language=lang,
                                                  existing_cases=(args.get("existing_names") or None)
                                                  if feedback else None,
                                                  defense_v2=config.DEFENSE_V2_ENABLED,
                                                  sut_functions=sut_manifest))])
    log_llm_call(task_id, phase, resp)
    data = _parse_cases(resp.content)
    if data is None:
        # 可观测性（批4 12/12 全降级实锤的教训）：解析失败必须留原始输出，否则
        # "诚实降级"了却永远查不了因——证据蒸发与 task_366 stderr 丢弃同型
        import time as _t
        tail = (resp.content or "")[-500:]
        log.warning("gen_tests task=%s LLM 输出不可解析（%d 字符），尾部：%s",
                    task_id, len(resp.content or ""), tail[:200].replace("\n", " ⏎ "))
        fail_dir = config.DATA_DIR / "reports" / "llm_parse_fail"
        fail_dir.mkdir(parents=True, exist_ok=True)
        (fail_dir / f"task{task_id}_{phase}_{_t.strftime('%H%M%S')}.txt").write_text(
            resp.content or "", encoding="utf-8")
        return json.dumps({"generated": 0,
                           "reason": "LLM 返回无法解析为用例（诚实降级，本轮不生成）；"
                                     f"原始输出已留档 llm_parse_fail/task{task_id}/，"
                                     f"尾部：{tail[:100]}"},
                          ensure_ascii=False)

    # —— 槽位模板展开（双通道）：基础方法 T1/T2/T5 渲染成代码，失败案留台账理由剔除 ——
    data["cases"], template_rejected = expand_template_cases(data["cases"])
    if template_rejected:
        log.info("gen_tests task=%s 模板展开剔除: %s", task_id, template_rejected)
    if not data["cases"] and template_rejected:
        return json.dumps({"generated": 0, "reason": "全部用例模板展开失败（槽位无效，诚实降级）",
                           "template_rejected": template_rejected}, ensure_ascii=False)

    # —— D8 期望依据校验：无需求依据的「需求明示」降级「模型推断」（假标注→假 product_defect 根因） ——
    basis_downgraded = _verify_expected_basis(data["cases"], args.get("requirement", ""))
    if basis_downgraded:
        log.info("gen_tests task=%s 需求明示降级 %d 案: %s",
                 task_id, len(basis_downgraded), basis_downgraded[:5])

    # —— 静态闸（§3.6 第 1 道）：语法 + from-import 符号存在性，坏案在写盘前剔除 ——
    data["cases"], rejected = _static_filter(data["cases"], ws)
    if not data["cases"]:
        return json.dumps({"generated": 0, "reason": "全部用例未过静态审查（诚实降级）",
                           "rejected": rejected, "template_rejected": template_rejected},
                          ensure_ascii=False)

    # —— def 名对齐（A5）：用规整后的 name 重写首个 def，nodeid 从此可反查用例 ——
    # 对话增补轮：防撞池=任务全域已有用例名（增补文件与已批准文件同名函数会让归因反查失准）
    # JS 分支：重写 test("…")/it("…") 的名字参数（jest nodeid=文件::用例名，与 Python 同口径）
    used_names: set[str] = set(args.get("existing_names") or [])
    for i, c in enumerate(data["cases"]):
        def_name = _sanitize_name(c.get("name"), i, used_names)
        code = c["code"].strip()
        if is_js:
            code = re.sub(r'(\b(?:test|it)\s*\(\s*)([\'"])[^\'"]*\2',
                          lambda m: f'{m.group(1)}"{def_name}"', code, count=1)
        else:
            m = re.search(r"def\s+\w+", code)
            if m:
                code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name

    # —— JS 路径脚手架确定性修正（task_366 实锤，先于写盘：落库/布考场重建同源）——
    root_fixed = 0
    if is_js:
        for c in data["cases"]:
            c["code"], n_fixed = _fix_js_scaffold_paths(c["code"])
            root_fixed += n_fixed
        if data.get("imports"):
            data["imports"], n_fixed = _fix_js_scaffold_paths(data["imports"])
            root_fixed += n_fixed
        if root_fixed:
            log.info("gen_tests task=%s JS 路径脚手架修正 %d 处（__dirname '..' → 仓库根）",
                     task_id, root_fixed)

    # —— 方案C·条件缺口回灌（Gotel&Finkelstein post-traceability 的生成侧闭环）：
    # 确定性 2-gram 映射查无用例对准的条件 → 定向补生成恰 1 轮（预算纪律）——
    # 与 V4 行覆盖回灌不同层：这里是 RTM 条件层的"覆盖了≠测到了"信号。
    # 对话增补轮跳过（增补目标由反馈文本定向，不做全量条件扫描）。 ——
    cond_feedback: dict | None = None
    if config.COND_FEEDBACK_ENABLED and conditions and not feedback:
        new_cases, extra_imports, cond_feedback = refill_condition_gaps(
            llm, sources, args.get("requirement", ""), conditions, data["cases"], ws,
            used_names, config.GEN_METHODS_ENABLED, config.PBT_ENABLED,
            task_id=task_id)
        if new_cases:
            data["cases"] += new_cases
            if extra_imports:
                extra_imports, _ = _fix_js_scaffold_paths(extra_imports)
                old = (data.get("imports") or "").strip()
                data["imports"] = (old + "\n" + extra_imports).strip()

    # —— 方案A·断言质量审计（度量恒算）+ 可选闸：全弱断言用例 = 无证伪能力 ——
    # JS 分支：审计器是 Python AST，对 jest 代码会整案判"语法不可解析"→ gate 开时全剔。
    # v1 跳过（stats 标 skipped 诚实留痕）；JS 断言质量闸属后续工作。
    if is_js:
        gate_rejected, assertion_stats = [], {"cases": len(data["cases"]), "skipped": "js_v1"}
    else:
        data["cases"], gate_rejected, assertion_stats = apply_assertion_gate(
            data["cases"], gate=config.ASSERTION_GATE_ENABLED)
    if gate_rejected:
        rejected += gate_rejected
    if not data["cases"]:
        return json.dumps({"generated": 0,
                           "reason": "全部用例未过断言质量闸（全弱断言，诚实降级）",
                           "rejected": rejected, "assertion": assertion_stats,
                           "cond_feedback": cond_feedback}, ensure_ascii=False)

    # —— §3.6 第 3 道闸：LLM 交叉语义审（V9，默认关；规则闸先行、LLM 闸收尾）——
    # 审查者角色逐案审断言是否对应需求，fail 单独重造 1 次、复审仍 fail 剔除，
    # 单案处置不拖累整批；审查不可解析 fail-open 放行（辅助闸不废整批生成）
    cross_info: dict | None = None
    if config.GEN_CROSS_REVIEW_ENABLED:
        data["cases"], cross_info = cross_review_gate(
            llm, data["cases"], args.get("requirement", ""), sources, ws,
            used_names, task_id=task_id)
        if not data["cases"]:
            return json.dumps({"generated": 0,
                               "reason": "全部用例未过交叉语义审（诚实降级）",
                               "rejected": rejected, "assertion": assertion_stats,
                               "cond_feedback": cond_feedback,
                               "cross_review": cross_info}, ensure_ascii=False)

    # —— 写入工作区（常规轮 test_gen_r{N}；对话增补轮独立 test_gen_fb{N}，不动已批准文件）
    # JS 用 .test.js 后缀（jest 默认 testMatch），framework='jest' ——
    round_no = args.get("round_no", 1)
    ext = ".test.js" if is_js else ".py"
    framework = "jest" if is_js else "pytest"
    if feedback and fb_no:
        fname = f"test_gen_fb{fb_no}{ext}"
        header_text = f"AI 增补用例（审查反馈第 {fb_no} 轮 · 两阶段评审对话）"
        version = int(args.get("version") or (100 + fb_no))
    else:
        fname = f"test_gen_r{round_no}{ext}"
        header_text = f"AI 生成用例（第 {round_no} 轮 · 五种方法知识库）"
        version = round_no
    header = f"// {header_text}" if is_js else f'"""{header_text}"""'
    imports = (data.get("imports") or "").strip()
    body = "\n\n".join(c["code"].strip() for c in data["cases"])
    content = (header + "\n"
               + (imports + "\n\n" if imports else "") + body + "\n")
    (ws / fname).write_text(content, encoding="utf-8")

    # —— 落库：脚本版本 + 用例清单（先于闸门，收集失败时可整体废案删除）；
    # filename 落列：两阶段"布考场"按审查结论重建生成文件的键 ——
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, module_id, framework, version, filename, content, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, args.get("module_id"), framework, version, fname, content,
         hashlib.sha256(content.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    for c in data["cases"]:
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content,
                                      priority, input, steps, expected, expected_basis)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (script_id, "ai", c.get("name", "unnamed"), c.get("method"), c["code"],
             c.get("priority"), c.get("input"), c.get("steps"),
             c.get("expected"), c.get("expected_basis")))
    conn.commit()

    # —— 回灌容器（依赖随项目走，生成物也随项目走）。
    # 两阶段准备图里 generate 先于 provision（阶段一出题无考场）：容器不存在时不炸，
    # collect 闸与回灌如实标注"顺延"，由执行图 provision 在快照后补跑（collect_gate_files）。 ——
    collect_deferred: str | None = None
    try:
        container = runner.client.containers.get(args["container_name"])
        runner.copy_dir_into(container, ws)
    except Exception as e:  # noqa: BLE001 —— 容器缺失/守护进程未起：阶段一出题的合法形态
        collect_deferred = f"容器未就绪（{type(e).__name__}），collect 闸顺延到阶段二布考场"

    # —— 第二道闸（§3.6 第 2 道）：容器内可收集性检查 ——
    # Python: pytest --collect-only；JS: node --check（语法闸，jest 收集=语法可解析+require 可达，
    # require 失败在执行轮按 ImportError 归因进 repair，不在此预���）。
    # 收集失败是文件级问题，无法按用例归责 → 整体废案（诚实降级），绝不把注定 ERROR 的生成物交给执行轮。
    r = None
    if collect_deferred is None:
        gate_cmd = (["node", "--check", fname] if is_js
                    else ["pytest", "--collect-only", "-q", fname])
        r = runner.exec_in(container, gate_cmd, timeout_s=120)
    if r is not None and r.exit_code != 0:
        (ws / fname).unlink(missing_ok=True)
        conn.execute("DELETE FROM test_cases WHERE script_id=?", (script_id,))
        conn.execute("DELETE FROM test_scripts WHERE id=?", (script_id,))
        conn.commit()
        runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)
        log.warning("gen_tests task=%s collect 失败，本轮生成物废案: %s",
                    task_id, r.stderr[-200:])
        return json.dumps({"generated": 0,
                           "reason": "生成物未过容器内 --collect-only 可收集性检查（整体废案）",
                           "detail": (r.stdout + r.stderr)[-300:]}, ensure_ascii=False)

    summary = {"generated": len(data["cases"]), "file": fname, "script_id": script_id,
               "static_rejected": rejected,
               # 用例明细（对话增补结果下拉的数据源）：每条是什么、验证什么、依据什么
               "cases": [{"name": c.get("name"), "method": c.get("method"),
                          "expected": (c.get("expected") or "")[:160],
                          "input": (c.get("input") or "")[:120],
                          "expected_basis": c.get("expected_basis")} for c in data["cases"]],
               "methods": [c.get("method") for c in data["cases"]],
               "template_rejected": template_rejected,
               "assertion": assertion_stats, "cond_feedback": cond_feedback,
               "root_fixed": root_fixed,
               "cross_review": cross_info, "source_skipped": source_skipped,
               "basis_downgraded": basis_downgraded, "phase": phase,
               "collect_deferred": collect_deferred,
               "conditions_truncated": conditions_truncated}
    log.info("gen_tests task=%s → %s", task_id, summary)
    return json.dumps(summary, ensure_ascii=False)


def rebuild_gen_files_from_db(conn, task_id: int, ws: Path) -> int:
    """两阶段"布考场"：按审查结论（test_cases.enabled）重写工作区生成用例文件。

    以 test_scripts.filename 为键逐文件重建：从脚本原文 AST 中摘除已停用用例的
    FunctionDef（保留 imports/模块级代码——imports 丢失会让整文件废），返回重写文件数。
    常规轮/对话增补轮文件都处理；无脚本行的任务返回 0（老流程不受影响）。
    """
    rows = conn.execute(
        """SELECT id, filename, content FROM test_scripts
           WHERE task_id=? AND framework IN ('pytest', 'jest') AND filename LIKE 'test_gen%'
           ORDER BY id""", (task_id,)).fetchall()
    rewritten = 0
    for row in rows:
        fname = row["filename"]
        if not fname:
            continue
        if not fname.endswith(".py"):
            # JS 文件无 Python AST：v1 不做用例级摘除，原样写回（评审勾选结论留在库台账，
            # 执行层对 JS 暂不生效——Tier2 诚实边界，README 已注明）
            (Path(ws) / fname).write_text(row["content"], encoding="utf-8")
            rewritten += 1
            continue
        enabled = {r["name"] for r in conn.execute(
            "SELECT name FROM test_cases WHERE script_id=? AND enabled=1", (row["id"],))}
        try:
            tree = ast.parse(row["content"])
        except SyntaxError:
            continue
        kept = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                if node.name in enabled:
                    kept.append(node)
                # 已停用用例的函数体摘除；非用例函数（helper）一律保留
            else:
                kept.append(node)
        tree.body = kept
        (Path(ws) / fname).write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        rewritten += 1
    return rewritten


def collect_gate_files(runner, container, conn, task_id: int, ws: Path) -> list[str]:
    """阶段一顺延的收集闸（两阶段执行图 provision 快照后补跑）：逐生成文件做容器内收集闸
    （Python: pytest --collect-only / JS: node --check），失败文件整体废案，返回废案文件名清单。
    test_imported.py 只报告不删库（人工插入用例保留审计，交由人工在评审页处理）。"""
    names = sorted(p.name for p in Path(ws).glob("test_gen_r*.py")) \
        + sorted(p.name for p in Path(ws).glob("test_gen_fb*.py")) \
        + sorted(p.name for p in Path(ws).glob("test_gen_*.test.js"))
    for imported_name in ("test_imported.py", "test_imported.test.js"):
        if (Path(ws) / imported_name).exists():
            names.append(imported_name)
    dropped: list[str] = []
    for fname in names:
        gate_cmd = (["node", "--check", fname] if fname.endswith(".test.js")
                    else ["pytest", "--collect-only", "-q", fname])
        r = runner.exec_in(container, gate_cmd, timeout_s=120)
        if r.exit_code == 0:
            continue
        dropped.append(fname)
        (Path(ws) / fname).unlink(missing_ok=True)
        runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)
        if fname != "test_imported.py":
            conn.execute(
                """DELETE FROM test_cases WHERE script_id IN
                   (SELECT id FROM test_scripts WHERE task_id=? AND filename=?)""",
                (task_id, fname))
            conn.execute("DELETE FROM test_scripts WHERE task_id=? AND filename=?",
                         (task_id, fname))
            conn.commit()
        log.warning("collect_gate_files task=%s 废案 %s: %s", task_id, fname,
                    (r.stderr or "")[-160:])
    return dropped
