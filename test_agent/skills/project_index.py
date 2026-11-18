# -*- coding: utf-8 -*-
"""项目知识索引 + 需求路由器（2026-11-18，设计方案-项目知识索引与需求路由-20261117.md）。

用户设想落地：全库扫描一次打标签（模块/符号/接口三清单持久化）→ 需求词秒定位
（别名路由）→ 未命中回退全库细找。主备双层=主方法（确定性索引+路由）+
L1 依赖闭包扩展/L2 覆盖率闭环兜底/L3 失败语料自学习（辅助/急救层）。

纪律：全部零 LLM 确定性；切分复用 module_profile（禁 LLM 定边界）；接口解析复用
endpoint_extract；别名命中落盘留痕（可审计）；未命中保守回退全库（不错路由）。
"""
import json
import os
import posixpath
import re
import time
from collections import defaultdict
from pathlib import Path

_INDEX_DIR_NAME = "index"

# ———————— 双语别名表（模块路由专用，L3 自学习会追加） ————————
_ALIAS_GROUPS: dict[str, list[str]] = {
    "user":       ["用户", "user", "账号", "account", "注册", "登录", "login", "auth", "认证"],
    "auth":       ["认证", "鉴权", "auth", "jwt", "token", "登录", "login", "oauth"],
    "order":      ["订单", "order", "booking", "预约", "预订", "reservation"],
    "pay":        ["支付", "pay", "payment", "checkout", "结算", "billing"],
    "product":    ["商品", "产品", "product", "item", "goods"],
    "cart":       ["购物车", "cart"],
    "inventory":  ["库存", "inventory", "stock"],
    "logger":     ["日志", "logger", "logging", "log"],
    "config":     ["配置", "config", "setting"],
    "db":         ["数据库", "database", "db", "repository", "dao", "model"],
    "api":        ["接口", "api", "endpoint", "rest", "路由", "route"],
    "notification": ["通知", "notification", "notify", "消息推送", "邮件", "邮件通知", "mail"],
    "search":     ["搜索", "search", "query", "查询"],
    "file":       ["文件", "file", "upload", "下载", "download"],
    "export":     ["导出", "export", "report", "报表"],
}

_HTTP_RE = re.compile(r"curl_init|fetch\(|axios|supertest|http://|https://|rest|端点|接口")

# 接口意图词组（只进 3) endpoints 对账，不参与 2) 模块别名匹配）
_ENDPOINT_INTENT_GROUPS = {"api"}


def _word_hit(word: str, blob: str) -> bool:
    """别名词→模块 blob 匹配：ASCII 词按词边界（金样 L3-4 实锤：子串语义让
    logger 组的 "log" ⊂ user 模块符号 "login"——"看下日志模块" 误路由到 user）；
    非 ASCII（中文）词保持子串语义（无边界可言且歧义低）。"""
    if word.isascii():
        return re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])",
                         blob) is not None
    return word in blob


def _walk_src(ws: Path, langs: list[str]) -> list[Path]:
    exts = {".py", ".js", ".ts", ".go", ".java", ".php"}
    out: list[Path] = []
    skip = {".git", "__pycache__", ".venv", "venv", "node_modules", "vendor", "dist",
            "build", "target", ".idea", ".pytest_cache", ".next", ".nuxt", "coverage",
            ".mypy_cache", ".gradle", "htmlcov"}
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for f in filenames:
            if Path(f).suffix.lower() in exts:
                out.append(Path(dirpath) / f)
    return sorted(out)


def _module_name(rel: Path) -> str:
    """路径→模块名：取首个有意义的目录段（user/auth/order…），根级文件归 _root。"""
    parts = [p for p in rel.parts[:-1] if p.lower() not in ("src", "lib", "app", "main", "api")]
    return parts[0].lower() if parts else "_root"


import os  # noqa: E402


def _build_callgraph(ws: Path, files: list[Path]) -> dict | None:
    """调用图邻接（L1 前置，文件级边，确定性零 LLM）：{"edges": [[caller, callee], ...]}。

    Python 走 AST（callgraph.build_call_edges 现有产物经 collect_functions 映射回文件）；
    JS/TS 走 import/require 相对路径正则；Go 走 go.mod 本模块前缀的 import→目录映射。
    一条边都建不出（语言不支持/图空）返回 None——索引不输出 callgraph 键，L1 自动降级。
    """
    edges: set[tuple[str, str]] = set()
    known = {f.relative_to(ws).as_posix() for f in files}

    # Python：AST 函数级边 → 文件级（复用 L2 白盒轨现有解析器，不新造轮子）
    if any(f.suffix == ".py" for f in files):
        try:
            from .callgraph import build_call_edges, collect_functions
            locs = collect_functions(ws)
            for caller, callees in build_call_edges(ws).items():
                cf = (locs.get(caller) or {}).get("file")
                if not cf:
                    continue
                for callee in callees:
                    tf = (locs.get(callee) or {}).get("file")
                    if tf and tf != cf:
                        edges.add((cf, tf))
        except Exception:                  # noqa: BLE001 —— 建图失败=L1 降级，不炸索引
            pass

    # JS/TS：import … from './x' / require('./x') / import('./x') → 同仓相对文件
    js_re = re.compile(
        r"""(?:import\s+[^'"()]*?from\s*|require\(\s*|import\(\s*)['"](\.[^'"]+)['"]""")
    js_exts = (".js", ".ts", ".jsx", ".tsx")
    for f in files:
        if f.suffix.lower() not in (".js", ".ts"):
            continue
        rel = f.relative_to(ws).as_posix()
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")[:100000]
        except OSError:
            continue
        for m in js_re.finditer(text):
            spec = m.group(1)
            base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
            cands = (base, *(base + e for e in js_exts),
                     *(f"{base}/index{e}" for e in js_exts))
            for cand in cands:
                if cand in known and cand != rel:
                    edges.add((rel, cand))
                    break

    # Go：go.mod 模块前缀的本地 import → 包目录首个已知 .go 文件
    go_files = [f for f in files if f.suffix == ".go"]
    if go_files:
        try:
            gomod = (ws / "go.mod").read_text(encoding="utf-8", errors="ignore")
            mm = re.search(r"^module\s+(\S+)", gomod, re.M)
            mprefix = mm.group(1) if mm else ""
        except OSError:
            mprefix = ""
        if mprefix:
            go_known = sorted(f.relative_to(ws).as_posix() for f in go_files)
            str_re = re.compile(r'"([^"]+)"')
            for f in go_files:
                rel = f.relative_to(ws).as_posix()
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")[:100000]
                except OSError:
                    continue
                iblock = re.search(r"import\s*\(([^)]*)\)", text, re.S)
                specs = str_re.findall(iblock.group(1)) if iblock else \
                    re.findall(r'^import\s+"([^"]+)"', text, re.M)
                for spec in specs:
                    if not spec.startswith(mprefix + "/"):
                        continue
                    sub = spec[len(mprefix) + 1:]
                    tgt = next((g for g in go_known
                                if g.startswith(sub + "/") or g == sub), None)
                    if tgt and tgt != rel:
                        edges.add((rel, tgt))

    if not edges:
        return None
    lst = [[a, b] for a, b in sorted(edges)]
    out = {"edges": lst}
    if len(lst) >= 20000:                  # 超大仓封顶（索引体积护栏，截断如实标注）
        out["truncated"] = True
        out["edges"] = lst[:20000]
    return out


def dependency_closure(index: dict, module: str, hops: int = 1,
                       callgraph: dict | None = None,
                       detail: dict | None = None) -> list[str]:
    """命中模块的依赖闭包（L1 辅助层，2026-11-18 设计方案 §1.2 改动 1）：
    返回应并入 scope 的模块名列表（[module] + 邻居，去重）。

    出边（module 调用的）与入边（调用 module 的）对端文件所属模块都是邻居；
    hops=2 在邻居集合上再迭代一轮（A→B→C 链上 C 只在 hops=2 出现）。
    防爆三护栏：_root（根级散文件）永不进闭包；邻居 >5 按 LOC 降序取 top5，
    其余记入 detail["skipped"]（留痕不静默）；callgraph 为 None（语言不支持/图空）
    → 只返回 [module]（降级=主方法行为，逐字节不变）。
    """
    mods = index.get("modules") or {}
    if module not in mods or not callgraph:
        if detail is not None:
            detail["skipped"] = []
            detail["degraded"] = module not in mods or not callgraph
        return [module]
    file2mod: dict[str, str] = {}
    for m, rec in mods.items():
        for fp in rec.get("files", []):
            file2mod[fp] = m
    all_edges = callgraph.get("edges") or []

    def _neighbors_of(mods_: set[str]) -> set[str]:
        out: set[str] = set()
        for cf, tf in all_edges:
            cm, tm = file2mod.get(cf), file2mod.get(tf)
            for src, dst in ((cm, tm), (tm, cm)):
                if src in mods_ and dst and dst != "_root" and \
                        not (src == module and dst == module):
                    out.add(dst)
        return out

    neighbors = {n for n in _neighbors_of({module}) if n != module}
    frontier = set(neighbors)
    for _ in range(max(0, int(hops) - 1)):
        newly = {n for n in _neighbors_of(frontier)
                 if n != module and n not in neighbors}
        neighbors |= newly
        frontier = newly
    ranked = sorted(neighbors,
                    key=lambda m: mods.get(m, {}).get("loc", 0), reverse=True)
    kept, skipped = ranked[:5], ranked[5:]
    if detail is not None:
        detail["skipped"] = skipped
        detail["hops"] = hops
    return [module] + kept


def build_index(ws: Path, langs: list[str]) -> dict:
    """全库扫描打标签（确定性零 LLM）：modules/symbols/endpoints 三清单+指纹。
    产出结构（data/index/project_<pid>.json 的 index 字段）。"""
    ws = Path(ws)
    files = _walk_src(ws, langs)
    modules: dict[str, dict] = defaultdict(lambda: {"files": [], "loc": 0, "symbols": []})
    endpoints: list[dict] = []

    # 接口解析：复用 endpoint_extract（openapi/go 路由）+ 轻量正则兜底
    try:
        from .endpoint_extract import parse_openapi, parse_go_routes
        for f in files:
            name = f.name.lower()
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")[:200000]
            except OSError:
                continue
            if name in ("openapi.json", "swagger.json") or name.startswith("openapi"):
                try:
                    spec = json.loads(text)
                    for ep in parse_openapi(spec):
                        endpoints.append({**ep, "source": f.name})
                except Exception:
                    pass
            elif f.suffix == ".go":
                for ep in parse_go_routes(text):
                    endpoints.append({**ep, "source": f.name})
    except Exception:
        pass

    # HTTP 兜底正则（node/express/php 路由，无 openapi/go 形态的项目）
    _HTTP_EP = re.compile(
        r"(?:app|router|router)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]",
        re.I)
    _PHP_EP = re.compile(r"\$(?:app|router)->(?:get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")

    sym_re = {
        ".py":  re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)"),
        ".js":  re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)|class\s+(\w+)"),
        ".ts":  re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)|class\s+(\w+)"),
        ".go":  re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),
        ".java": re.compile(r"public\s+(?:static\s+)?[\w<>\[\]]+\s+(\w+)\s*\("),
        ".php": re.compile(r"public\s+function\s+(\w+)"),
    }

    for f in files:
        rel = f.relative_to(ws)
        mod = _module_name(rel)
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        loc = text.count("\n") + 1
        mrec = modules.setdefault(mod, {"files": [], "loc": 0, "symbols": [],
                                        "language": f.suffix.lstrip(".").lower()})
        mrec["files"].append(rel.as_posix())
        mrec["loc"] += loc
        for ln in text.splitlines():
            for rx in (sym_re.get(f.suffix.lower()),):
                if rx:
                    m = rx.match(ln.strip())
                    if m:
                        mrec["symbols"].append(m.group(1) or m.group(2) or "")
                    break
        # HTTP 端点兜底（node/express 形态）
        for m in _HTTP_EP.finditer(text):
            endpoints.append({"method": m.group(1).upper(), "path": m.group(2),
                              "source": rel.as_posix(), "handler": ""})
        for m in _PHP_EP.finditer(text):
            endpoints.append({"method": "ANY", "path": m.group(1),
                              "source": rel.as_posix(), "handler": ""})

    for mod, rec in modules.items():
        rec["files_n"] = len(rec["files"])
        rec["symbols_n"] = len(rec["symbols"])

    fingerprint = f"{len(files)}:{sum(f.stat().st_size for f in files)}:{int(time.time())}"
    out = {"modules": {k: v for k, v in modules.items()},
           "endpoints": endpoints,
           "fingerprint_src": fingerprint,
           "built_at": time.strftime("%F %T")}
    # L1 前置：调用图邻接（建不出图=无 callgraph 键，dependency_closure 自动降级）
    cg = _build_callgraph(ws, files)
    if cg:
        out["callgraph"] = cg
    return out


def match_requirement(index: dict, requirement: str) -> dict:
    """需求→索引路由（三层置信）：模块名直接命中=high；别名组命中=medium；
    接口词=kind endpoints；未命中=none（回退全库，绝不因路由缩小视野）。

    无范围可锁的模块不参与路由（文件数=全库总文件数的模块，如单目录仓的顶层
    目录名——锁它=锁全库=白锁还污染接口意图；E2E-1 预演实锤：go-rest-api-master
    单模块把"测所有 REST 接口"的 rest 别名吞进模块路由，接口对账从此失效）。"""
    req = (requirement or "").lower()
    mods = index.get("modules") or {}
    total_files = sum(len(rec.get("files", [])) for rec in mods.values())

    def _routable(mod: str, rec: dict) -> bool:
        return (mod != "_root" and mods and
                len(rec.get("files", [])) < total_files)

    # 1) 模块名直接命中（路径段或模块名出现在需求里）
    for mod, rec in mods.items():
        if _routable(mod, rec) and mod.lower() in req:
            return {"kind": "module", "module": mod, "confidence": "high",
                    "paths": rec["files"]}
    # 2) 别名组 → 模块（找模块名含别名或模块符号含别名）
    for group, words in _ALIAS_GROUPS.items():
        if group in _ENDPOINT_INTENT_GROUPS:
            continue   # api 组=接口意图词非领域别名（E2E-1 预演实锤：internal 模块
                       # blob 里一个 "rest" 词把"测所有 REST 接口"吞进模块路由；
                       # 接口意图统一走 3) endpoints 对账；模块名直中仍走 1)）
        if not any(w in req for w in words):
            continue
        for mod, rec in mods.items():
            if not _routable(mod, rec):
                continue
            blob = (mod + " " + " ".join(rec.get("symbols", [])) + " " +
                    " ".join(rec.get("files", []))).lower()
            if any(_word_hit(w, blob) for w in words):
                return {"kind": "module", "module": mod, "confidence": "medium",
                        "paths": rec["files"], "alias_group": group}
    # 3) 接口词 → endpoints 全量
    if any(w in req for w in ("接口", "api", "endpoint", "rest")):
        eps = index.get("endpoints") or []
        if eps:
            return {"kind": "endpoints", "endpoints": eps, "count": len(eps)}
    return {"kind": "none"}


def coverage_check(state: dict, per_file: list[dict],
                   coverage_line: float | None = None) -> dict:
    """L2 覆盖率闭环兜底（跑后钩子，2026-11-18 设计方案 §2.2 改动 1）：路由锁定的
    范围有没有真覆盖。per_file=evaluate 产出的逐文件覆盖行（term-missing 表）。

    表语义（关键前提）：--cov-report=term-missing:skip-covered 会**省略全覆盖文件**，
    0% 文件必在表内——据此把"范围内文件缺席缺口表"与 TOTAL 联合判读：
    - state 无 index_scope_paths → not_routed（非路由格，跳过）；
    - 范围内行存在且任一 cover>0 → ok（标签正确，结束）；
    - 范围内行全 0 → zero_coverage（文件被执行但零覆盖=标签漏了）；
    - 范围内文件缺席 + TOTAL>0 → ok（skip-covered 省略=已覆盖）；
    - 范围内文件缺席 + TOTAL=0 → zero_coverage（什么都没测到）；
    - 范围内文件缺席 + TOTAL 缺失 → no_data（测不了就不猜，急救不触发——诚实出口）。
    """
    scope = state.get("index_scope_paths") or []
    if not scope:
        return {"verdict": "not_routed"}
    prefixes = [s.replace("\\", "/").rstrip("/") for s in scope if s]

    def _in_scope(fp: str) -> bool:
        f = (fp or "").replace("\\", "/")
        return any(f == p or f.startswith(p + "/") for p in prefixes)

    rows = [r for r in (per_file or []) if _in_scope(str(r.get("file", "")))]
    if rows:
        if any(int(r.get("cover") or 0) > 0 for r in rows):
            return {"verdict": "ok", "scope_rows": len(rows)}
        return {"verdict": "zero_coverage", "scope_rows": len(rows),
                "detail": "范围内文件全部 0 覆盖"}
    if coverage_line is None:
        return {"verdict": "no_data",
                "detail": "覆盖数据缺失（无覆盖机制或 TOTAL 解析失败）——不猜，急救不触发"}
    if coverage_line > 0:
        return {"verdict": "ok",
                "detail": "范围内文件不在缺口表且 TOTAL>0（skip-covered 省略全覆盖）=已覆盖"}
    return {"verdict": "zero_coverage",
            "detail": "范围内文件缺席缺口表且 TOTAL=0——什么都没测到"}


def ensure_index(pid: int, ws: Path, langs: list[str], force: bool = False) -> dict:
    """构建/复用索引：指纹不变跳过重建（一劳永逸语义）。落盘 data/index/project_<pid>.json。"""
    out = _index_dir() / f"project_{pid}.json"
    if out.exists() and not force:
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            pass
    idx = build_index(ws, langs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return idx


def _index_dir() -> Path:
    """索引目录（调用时读 config.DATA_DIR——跟随测试/环境的 DATA_DIR 覆盖）。"""
    env = os.environ.get("MTA_INDEX_DIR")
    if env:
        return Path(env)
    from .. import config
    return Path(config.DATA_DIR) / "index"
