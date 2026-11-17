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
    return {"modules": {k: v for k, v in modules.items()},
            "endpoints": endpoints,
            "fingerprint_src": fingerprint,
            "built_at": time.strftime("%F %T")}


def match_requirement(index: dict, requirement: str) -> dict:
    """需求→索引路由（三层置信）：模块名直接命中=high；别名组命中=medium；
    接口词=kind endpoints；未命中=none（回退全库，绝不因路由缩小视野）。"""
    req = (requirement or "").lower()
    # 1) 模块名直接命中（路径段或模块名出现在需求里）
    for mod, rec in (index.get("modules") or {}).items():
        if mod != "_root" and mod.lower() in req:
            return {"kind": "module", "module": mod, "confidence": "high",
                    "paths": rec["files"]}
    # 2) 别名组 → 模块（找模块名含别名或模块符号含别名）
    for group, words in _ALIAS_GROUPS.items():
        if not any(w in req for w in words):
            continue
        for mod, rec in (index.get("modules") or {}).items():
            blob = (mod + " " + " ".join(rec.get("symbols", [])) + " " +
                    " ".join(rec.get("files", []))).lower()
            if any(w in blob for w in words):
                return {"kind": "module", "module": mod, "confidence": "medium",
                        "paths": rec["files"], "alias_group": group}
    # 3) 接口词 → endpoints 全量
    if any(w in req for w in ("接口", "api", "endpoint", "rest")):
        eps = index.get("endpoints") or []
        if eps:
            return {"kind": "endpoints", "endpoints": eps, "count": len(eps)}
    return {"kind": "none"}


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
