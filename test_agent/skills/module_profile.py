"""模块级画像与策略建议 A 版（任务卡-E，2026-11-09）：只"看与建议"，不改执行链。

同一项目中不同模块适用不同测试方法（用户原话）。A 版边界（已拍板）：产出
模块×特征×建议路线×理由，执行接线（generate/run 限定模块）是 B 版=未来工作；
答辩只答"能画像能解释"，不答"分配比均分好"（那是 B 版对照实验）。

三层全部确定性优先：
1. **模块切分**（绝不让 LLM 定边界）：Python=含 __init__.py 目录（否则顶层散文件）；
   JS/TS=src 下一级目录（否则逐文件）；Go=含 .go 的 package 目录；HTML=每页面一
   模块；按 (复杂度×2+LOC) 排序取 Top 15，其余归 excluded 如实标注（防大仓爆预算）。
2. **9 维画像**（全静态可得）：语言/类型/LOC/文件数/圈复杂度（radon，缺失=0 诚实）/
   扇入度（被 import 次数=核心权重）/自带测试有无/Web 入口/DB 调用/纯数据判定。
3. **规则表**（卡 §4 七条，逐条可单测）：纯数据→跳过（省预算）；页面→UI 幂等；
   API→契约+场景；DB→单元+db_assert；纯函数+docstring→单元；纯函数无规格→
   属性+差分；复杂度≥C 且扇入≥3→权重×2。规则未命中才 LLM 语义分类一次
   （单次批量调用，fail-open 且理由落库——可审计）。
"""
import ast
import json
import logging
import re
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

TOP_LIMIT = 15
_SKIP = set(config.SKIP_DIRS)
_HOT_RANKS = {"C", "D", "E", "F"}
_RE_ROUTE_DECOR = re.compile(r"@(app|router|\w+)\.(get|post|put|delete|patch|route)\(", re.M)
# Python 的 Web 入口用 AST 判定（装饰器须真挂在函数上）——全文正则会把注释里的
# 路由字样当入口（实测误判：注释 @app.get + 常量 → 被判 api_handler）
_ROUTE_ATTRS = {"get", "post", "put", "delete", "patch", "route", "websocket"}
_RE_DB_CALL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|EXECUTE)\b|\.execute\(|sqlite3|cursor", re.I)
_RE_JS_FUNC = re.compile(r"\bfunction\s+\w+\s*\(|=>|\bexport\s+(?:const|function|class)\b")
_RE_GO_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?\w+\s*\(", re.M)


# —————————————————————————— 1. 模块切分（确定性） ——————————————————————————

def _py_files(ws: Path) -> list[Path]:
    return [p for p in sorted(ws.rglob("*.py")) if not any(part in _SKIP for part in p.parts)]


def _file_loc(p: Path) -> int:
    try:
        return sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
    except OSError:
        return 0


def _file_complexity(p: Path) -> int:
    """radon 缺失/语法错 → 0（诚实，不阻断）。"""
    try:
        from radon.complexity import cc_visit
        code = p.read_text(encoding="utf-8", errors="ignore")
        return sum(int(b.complexity) for b in cc_visit(code))
    except Exception:                              # noqa: BLE001 —— ImportError/SyntaxError/OSError
        return 0


def _rank_key(ws: Path, files: list[Path]) -> tuple:
    loc = sum(_file_loc(f) for f in files)
    cplx = sum(_file_complexity(f) for f in files)
    return cplx * 2 + loc


def split_modules(ws, lang: str) -> tuple[list[dict], list[dict]]:
    """确定性切分。返回 (kept, excluded)：每项 {path, files:[posix rel]}。
    kept 按 (复杂度×2+LOC) 降序取 Top 15；excluded 如实标注（防 200 文件仓爆预算）。"""
    ws = Path(ws)
    if not ws.exists():
        return [], []
    groups: dict[str, list[Path]] = {}
    if lang == "python":
        py = _py_files(ws)
        pkg_dirs = {p.parent for p in py if p.name == "__init__.py"}
        for p in py:
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                continue
            if p.parent in pkg_dirs:
                key = p.parent.relative_to(ws).as_posix()   # 包目录=一模块
            else:
                key = p.relative_to(ws).as_posix()          # 非包散文件=每文件一模块
            groups.setdefault(key, []).append(p)
    elif lang in ("javascript", "typescript"):
        src = ws / "src"
        if src.is_dir():
            for d in sorted(src.iterdir()):
                if d.is_dir():
                    groups[d.relative_to(ws).as_posix()] = [
                        p for p in sorted(d.rglob("*")) if p.is_file()]
        if not groups:
            for p in sorted(ws.rglob("*.js")) + sorted(ws.rglob("*.ts")):
                if not any(part in _SKIP for part in p.parts):
                    groups.setdefault(p.parent.relative_to(ws).as_posix(), []).append(p)
    elif lang == "go":
        for p in sorted(ws.rglob("*.go")):
            if not any(part in _SKIP for part in p.parts):
                groups.setdefault(p.parent.relative_to(ws).as_posix(), []).append(p)
    elif lang in ("html", "unknown"):
        for p in sorted(ws.rglob("*.htm*")):
            if not any(part in _SKIP for part in p.parts):
                groups.setdefault(p.relative_to(ws).as_posix(), [p])
    # 页面模块跨语言追加（卡 §1：HTML/页面=每页面一模块——python 主语言项目里的
    # web/index.html 也必须成为独立模块，否则页面路线永不参与）
    if lang not in ("html", "unknown"):
        for p in sorted(ws.rglob("*.htm*")):
            if any(part in _SKIP for part in p.parts):
                continue
            key = p.relative_to(ws).as_posix()
            groups.setdefault(key, []).append(p)
    if not groups:                                     # 兜底：无法切分=整仓一模块
        files = [p for p in sorted(ws.rglob("*")) if p.is_file()
                 and not any(part in _SKIP for part in p.parts)]
        groups = {"." : files} if files else {}
    ranked = sorted(groups.items(), key=lambda kv: -_rank_key(ws, kv[1]))
    kept = [{"path": k, "files": [f.relative_to(ws).as_posix() for f in fs]}
            for k, fs in ranked[:TOP_LIMIT]]
    excluded = [{"path": k, "files": len(fs)}
                for k, fs in ranked[TOP_LIMIT:]]
    return kept, excluded


# —————————————————————————— 2. 九维画像 ——————————————————————————

def profile_module(ws: Path, mod: dict, all_modules: list[dict],
                   lang: str = "python") -> dict:
    """9 维画像（全静态）。kind ∈ pure_func/api_handler/db_access/page/config/mixed。"""
    ws = Path(ws)
    rels = mod["files"]
    loc = sum(_file_loc(ws / r) for r in rels)
    complexity = sum(_file_complexity(ws / r) for r in rels)
    # 扇入：其他模块的源文件 import 本模块（包目录名 ∪ 非 __init__ 的文件 stem）
    mod_names = ({Path(mod["path"]).stem}
                 | {Path(r).stem for r in rels if Path(r).stem != "__init__"})
    fan_in = 0
    for other in all_modules:
        if other["path"] == mod["path"]:
            continue
        for r in other["files"]:
            if not r.endswith(".py"):
                continue
            try:
                tree = ast.parse((ws / r).read_text(encoding="utf-8", errors="ignore"))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                if any(n in mod_names for n in names):
                    fan_in += 1
                    break
    web_entry = False
    db_calls = False
    has_docstring = False
    has_functions = False
    py_route_decorated = False
    for r in rels:
        p = ws / r
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if r.endswith(".html"):
            web_entry = True
        if _RE_DB_CALL.search(text):
            db_calls = True
        if r.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            if ast.get_docstring(tree):
                has_docstring = True
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    has_functions = True
                    for dec in node.decorator_list:
                        name = (dec.func.attr if isinstance(dec, ast.Call)
                                and isinstance(dec.func, ast.Attribute)
                                else dec.attr if isinstance(dec, ast.Attribute)
                                else getattr(dec, "attr", None))
                        if name in _ROUTE_ATTRS:
                            py_route_decorated = True
        elif r.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")):
            # JS/TS：函数检测用轻量正则（Python AST 不覆盖）；docstring 信号=JSDoc 块
            if _RE_JS_FUNC.search(text):
                has_functions = True
            if "/**" in text:
                has_docstring = True
        elif r.endswith(".go"):
            # Go：func 声明正则；docstring 信号=godoc 注释（导出名上方 "// Name …"）
            if _RE_GO_FUNC.search(text):
                has_functions = True
            if re.search(r"//\s*[A-Z]\w+", text):
                has_docstring = True
    if py_route_decorated:
        web_entry = True
    mdir = ws / mod["path"]
    own_tests = bool(mdir.is_dir() and list(mdir.glob("test_*.py")))
    pure_data = (not has_functions and not web_entry)
    if pure_data:
        kind = "config"
    elif web_entry and py_route_decorated:
        kind = "api_handler"
    elif rels and all(r.endswith(".html") for r in rels):
        kind = "page"
    elif db_calls:
        kind = "db_access"
    elif has_functions:
        kind = "pure_func"
    else:
        kind = "mixed"
    return {"language": lang, "kind": kind, "loc": loc, "file_count": len(rels),
            "complexity": complexity, "fan_in": fan_in,
            "has_own_tests": bool(own_tests), "web_entry": web_entry,
            "db_calls": db_calls, "pure_data": pure_data,
            "has_docstring": has_docstring, "has_functions": has_functions}


# —————————————————————————— 3. 策略规则表（卡 §4 七条，逐条可测） ——————————————————————————

def suggest_routes(profile: dict) -> tuple[list[dict], str]:
    """规则表：返回 (建议列表, 命中理由)。未命中返回 ([], "")（调用方走 LLM 兜底）。"""
    sugg: list[dict] = []
    reasons = []
    weight = 1.0
    if profile["pure_data"]:
        return [{"route": "skip", "reason": "纯数据/配置，无可测行为", "weight": 0.0}], \
            "规则[纯数据跳过]：无函数无页面入口，标注『无可测行为』省预算"
    if profile["kind"] == "page" or (profile["web_entry"] and not profile["has_functions"]):
        sugg.append({"route": "ui_interaction_idempotency", "weight": 1.0,
                     "reason": "页面只能测外部行为（任务卡-D 幂等路线：单击/双击不变量）"})
        reasons.append("规则[HTML 页面→UI 交互]")
    if profile["kind"] == "api_handler":
        sugg.append({"route": "l2_contract_http", "weight": 1.0,
                     "reason": "检测到路由装饰器，接口契约可提取（契约+场景）"})
        reasons.append("规则[API handler→契约+场景]")
    if profile["db_calls"]:
        sugg.append({"route": "db_assert", "weight": 1.0,
                     "reason": "检测到 SQL 调用，需验证状态变化（单元+db_assert）"})
        sugg.append({"route": "l1_unit_pytest", "weight": 0.8,
                     "reason": "DB 访问层通常含可单测的封装函数"})
        reasons.append("规则[DB 写入→单元+db_assert]")
    if profile["kind"] == "pure_func" and profile["has_docstring"]:
        sugg.append({"route": "l1_unit_pytest", "weight": 1.0,
                     "reason": "纯函数且有 docstring，输入输出契约清晰（等价类/边界/PBT）"})
        reasons.append("规则[纯函数+有规格→单元]")
    elif profile["kind"] == "pure_func" and not profile["has_docstring"]:
        sugg.append({"route": "property_based", "weight": 0.9,
                     "reason": "纯函数无规格（无 docstring），无预期值可写→属性+差分"})
        sugg.append({"route": "cli_reference_diff", "weight": 0.7,
                     "reason": "无规格场景以参照物为 oracle（差分路线）"})
        reasons.append("规则[纯函数+无规格→属性+差分]")
    if profile["complexity"] >= 10 and profile["fan_in"] >= 3:
        weight = 2.0
        reasons.append(f"规则[复杂度≥C 且扇入≥3→权重×2]（复杂度 {profile['complexity']}"
                       f"、扇入 {profile['fan_in']}）")
    for s in sugg:
        s["weight"] = round(s["weight"] * weight, 2)
    return sugg, ("；".join(reasons) if reasons else "")


# —————————————————————————— 4. LLM 兜底（规则未命中，单次批量，fail-open） ——————————————————————————

def classify_fallback(ws: Path, mods: list[dict], profiles: list[dict],
                      llm_fn=None) -> dict[int, dict]:
    """规则未命中的模块：LLM 语义分类一次（'业务规则/工具/胶水'），理由落库。
    llm_fn(prompt)->str 注入可测；失败/不可用→理由如实记 llm_unavailable（fail-open，
    绝不编造）。返回 {index: {"kind_label":…, "reason":…}}。"""
    unhit = [i for i, (m, p) in enumerate(zip(mods, profiles))
             if not p.get("_has_suggestion")]
    if not unhit:
        return {}
    if llm_fn is None:
        return {i: {"kind_label": "unclassified",
                    "reason": "规则未命中且 LLM 兜底不可用（fail-open，如实留痕不编造）"}
                for i in unhit}
    listing = "\n".join(
        f"{i}. path={mods[i]['path']} files={mods[i]['files'][:5]} "
        f"loc={profiles[i]['loc']}" for i in unhit)
    prompt = ("对下列软件模块做语义分类（业务规则/工具/胶水 之一），输出 JSON："
              '{"0": {"kind_label": "...", "reason": "一句话"}, ...}\n' + listing)
    try:
        raw = llm_fn(prompt)
        data = json.loads(raw)
        out = {}
        for i in unhit:
            item = data.get(str(i)) or {}
            out[i] = {"kind_label": str(item.get("kind_label") or "unclassified")[:20],
                      "reason": f"LLM 兜底分类：{str(item.get('reason') or '')[:160]}"}
        return out
    except Exception as e:                          # noqa: BLE001 —— fail-open 如实留痕
        return {i: {"kind_label": "unclassified",
                    "reason": f"LLM 兜底失败（{type(e).__name__}），如实留痕不编造"}
                for i in unhit}


# —————————————————————————— 5. 入口：画像全流程 + 落库 ——————————————————————————

def analyze_modules(ws, lang: str, llm_fn=None) -> dict:
    """切分→画像→建议→兜底。返回可直接 persist 的报告（含 excluded 如实标注）。"""
    ws = Path(ws)
    kept, excluded = split_modules(ws, lang)
    mods, profiles = [], []
    for m in kept:
        p = profile_module(ws, m, kept, lang=lang)
        sugg, reason = suggest_routes(p)
        p["_has_suggestion"] = bool(sugg)
        mods.append(m)
        profiles.append(p)
    fallback = classify_fallback(ws, mods, profiles, llm_fn)
    out = {"modules": [], "excluded": excluded,
           "note": f"Top {TOP_LIMIT} 上限外 {len(excluded)} 模块未纳入（防预算爆炸，如实标注）"
                   if excluded else ""}
    for i, (m, p) in enumerate(zip(mods, profiles)):
        sugg, rule_reason = suggest_routes(p)
        fb = fallback.get(i)
        reason = rule_reason or (fb["reason"] if fb else "")
        out["modules"].append({"path": m["path"], "profile": {
            k: p[k] for k in ("language", "kind", "loc", "file_count", "complexity",
                              "fan_in", "has_own_tests", "web_entry", "db_calls",
                              "pure_data")},
            "suggested_routes": sugg, "reason": reason,
            "weight": max((s["weight"] for s in sugg), default=0.0),
            "kind_label": fb["kind_label"] if fb else ""})
    return out


def persist(conn, pid: int, report: dict) -> int:
    """落库（幂等：同项目清后重写）。modules 追加逐模块行（'.' 整仓行保留，
    test_scripts.module_id 的 FK 不受影响）+ module_profiles 第 34 表。"""
    conn.execute("DELETE FROM module_profiles WHERE project_id=?", (pid,))
    n = 0
    for m in report["modules"]:
        conn.execute(
            """INSERT INTO modules(project_id, path, language, loc, strategy)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id, path) DO UPDATE SET language=excluded.language,
                 loc=excluded.loc, strategy=excluded.strategy""",
            (pid, m["path"], m["profile"]["language"], m["profile"]["loc"],
             "skip" if m["profile"]["pure_data"] else "full"))
        conn.execute(
            """INSERT INTO module_profiles(project_id, path, profile_json,
                 suggested_routes, reason, weight) VALUES (?,?,?,?,?,?)""",
            (pid, m["path"], json.dumps(m["profile"], ensure_ascii=False),
             json.dumps(m["suggested_routes"], ensure_ascii=False),
             m["reason"][:500], m["weight"]))
        n += 1
    conn.commit()
    return n
