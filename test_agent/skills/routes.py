"""多测试路线编排（期 A，2026-11-09）：声明式路线注册表 + 确定性路由器。

设计原则（任务卡纪律）：
1. **preconditions 由确定性代码判定**（文件特征/语言/清单），LLM 不得越过前置条件
   选择路线——LLM 只能在「通过前置条件的候选集」内排序/给理由（期 B 的两臂实验对象）。
2. **每条候选路线必须产出 选/不选 + 理由**——被剔除的也记录原因（可审计），
   落 route_plan 表（第 33 表）供报告页/论文呈现。
3. **统一结果 schema**：各路线执行产物对齐 evaluate/attribution 的输入契约
   （pytest 风格文本 + test_case_results 行 + attribution 三分类），禁止各自造解析。
4. 新能力旗标 MTA_ROUTER 默认关：关闭时 plan 行为逐字节不变。
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# —————————————————————————— 项目特征提取（确定性，零 LLM） ——————————————————————————

_PY_RE = re.compile(r"\.py$")
_JS_RE = re.compile(r"\.(js|mjs|cjs|ts|tsx)$")
_HTML_RE = re.compile(r"\.html?$")
_PROTO_RE = re.compile(r"\.proto$")
_SQL_RE = re.compile(r"\.(sql|db|sqlite|sqlite3)$", re.I)
_INTERACTIVE_RE = re.compile(
    r"<button\b|<input[^>]+type=[\"'](?:submit|button)[\"']|<a[^>]+onclick", re.I)
_FORM_RE = re.compile(r"<form\b|<table\b|<ul\b|<ol\b", re.I)
_HTTP_FRAMEWORK_RE = re.compile(
    r"from\s+(fastapi|flask|django|starlette|aiohttp)|import\s+(fastapi|flask)", re.M)
_SKIP = {".git", "__pycache__", "node_modules", "vendor", ".venv", "venv", "dist", "build"}


def extract_route_features(ws: str | Path, max_samples: int = 400) -> dict:
    """确定性提取路由判据特征（零 LLM）：文件类型计数 + 交互/表单特征 + HTTP 框架。"""
    ws = Path(ws)
    f = {"py": 0, "js": 0, "html": 0, "proto": 0, "sql": 0,
         "has_forms": False, "has_interactive": False,
         "has_http_framework": False, "has_package_json": False,
         "has_go_mod": False, "samples": 0}
    if not ws.exists():
        return f
    pkg = ws / "package.json"
    f["has_package_json"] = pkg.is_file()
    f["has_go_mod"] = (ws / "go.mod").is_file()
    http_sampled = 0
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ws)
        if any(part in _SKIP for part in rel.parts):
            continue
        f["samples"] += 1
        s = str(p.suffix.lower())
        if _PY_RE.match(s):
            f["py"] += 1
        elif _JS_RE.match(s):
            f["js"] += 1
        elif _HTML_RE.match(s):
            f["html"] += 1
        elif _PROTO_RE.match(s):
            f["proto"] += 1
        elif _SQL_RE.match(s):
            f["sql"] += 1
        if s == ".html" or s == ".htm":
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")[:20000]
                if _INTERACTIVE_RE.search(t):
                    f["has_interactive"] = True
                if _FORM_RE.search(t):
                    f["has_forms"] = True
            except OSError:
                pass
        elif s == ".py" and http_sampled < 120:
            http_sampled += 1
            try:
                head = p.read_text(encoding="utf-8", errors="ignore")[:8000]
                if _HTTP_FRAMEWORK_RE.search(head):
                    f["has_http_framework"] = True
            except OSError:
                pass
    return f


# —————————————————————————— 路线注册表（声明式） ——————————————————————————

@dataclass
class Route:
    id: str
    name: str
    preconditions: list      # [(判据函数名, 参数)]，evaluate_route 逐条判定
    toolchain: dict          # {image, skill}
    oracle_type: str         # 对齐 attribution 语义的判定来源
    cost: str                # low / medium / high
    priority: int = 50       # 同通过集内排序（小者优先）


def _c_lang_eq(f: dict, lang: str) -> tuple[bool, str]:
    ok = f.get("language") == lang
    return ok, (f"主语言={lang}" if ok else f"主语言={f.get('language')}，需 {lang}")


def _c_min_files(f: dict, key: str, n: int) -> tuple[bool, str]:
    ok = f.get(key, 0) >= n
    return ok, (f"{key} 文件 ≥{n}（实际 {f.get(key, 0)}）"
                if ok else f"{key} 文件不足（实际 {f.get(key, 0)}，需 ≥{n}）")


def _c_flag(f: dict, key: str) -> tuple[bool, str]:
    ok = bool(f.get(key))
    return ok, (f"检测到 {key}" if ok else f"未检测到 {key}")


def _c_always(f: dict) -> tuple[bool, str]:
    return True, "兜底路线（永真前置）"


def _c_lang_known(f: dict) -> tuple[bool, str]:
    ok = f.get("language") not in (None, "", "unknown")
    return ok, ("主语言已识别" if ok else "主语言 unknown（无可执行路线的最低前提）")


# 每条路线的前置条件：全部满足才可执行；任一不满足即剔除并记录原因
ROUTES: list[Route] = [
    Route("l1_unit_pytest", "L1 单元测试（pytest）",
          [("lang_eq", "python"), ("min_files", ("py", 1))],
          {"image": "config:IMAGE_PY", "skill": "gen_tests"}, "unit_assert", "medium", 10),
    Route("l2_contract_http", "L2 契约/HTTP（httptest·TestClient）",
          [("lang_eq", "python"), ("flag", "has_http_framework")],
          {"image": "config:IMAGE_PY", "skill": "l2"}, "contract", "medium", 20),
    Route("l3_scenario", "L3 场景测试",
          [("lang_eq", "python"), ("flag", "has_http_framework")],
          {"image": "config:IMAGE_PY", "skill": "l3"}, "scenario", "high", 30),
    Route("l4_acceptance", "L4 验收（GWT 清单）",
          [("lang_eq", "python"), ("flag", "has_http_framework")],
          {"image": "config:IMAGE_PY", "skill": "l4"}, "acceptance", "high", 40),
    Route("ui_static_dom", "UI 静态 DOM（ui_assert·Playwright）",
          [("min_files", ("js", 1))],
          {"image": "config:IMAGE_UI", "skill": "ui_assert"}, "dom_snapshot", "medium", 20),
    Route("ui_interaction_idempotency", "UI 交互幂等（单击/双击/三连击不变量）",
          [("min_files", ("html", 1)), ("flag", "has_interactive")],
          {"image": "config:IMAGE_UI", "skill": "ui_idempotency"},
          "state_invariant", "low", 10),
    Route("db_assert", "数据库断言（db_assert·sidecar）",
          [("min_files", ("sql", 1))],
          {"image": "config:IMAGE_PY", "skill": "db_assert"}, "db_query", "low", 30),
    Route("grpc_contract", "gRPC 契约（grpc_assert·grpcurl）",
          [("min_files", ("proto", 1))],
          {"image": "config:IMAGE_GRPC", "skill": "grpc_assert"}, "rpc_contract", "medium", 30),
    Route("static_scan", "静态扫描（兜底，无执行）",
          [("always", None)],
          {"image": "", "skill": "static_scan"}, "static_draft", "low", 90),
]

_BY_ID = {r.id: r for r in ROUTES}

_COND_FN = {
    "lang_eq": lambda f, a: _c_lang_eq(f, a),
    "min_files": lambda f, a: _c_min_files(f, a[0], a[1]),
    "flag": lambda f, a: _c_flag(f, a),
    "always": lambda f, a: _c_always(f),
}


# —————————————————————————— 路由器 ——————————————————————————

def evaluate_route(route: Route, features: dict) -> tuple[bool, str]:
    """逐条判定前置条件：全过=可选；任一不过=剔除（返回首个不满足的原因，可审计）。"""
    reasons = []
    for name, arg in route.preconditions:
        ok, why = _COND_FN[name](features, arg)
        if not ok:
            return False, f"前置不满足[{name}]：{why}"
        reasons.append(why)
    return True, "；".join(reasons)


def route_plan(requirement: str, features: dict, language: str | None = None,
               conn=None, task_id: int | None = None,
               flags: dict | None = None) -> dict:
    """确定性路由器：需求+特征 → 全路线的 选/不选+理由+优先级。

    返回 {routes: [{route_id, name, selected, reason, priority, oracle_type, cost}],
          selected_ids: [...]}。语言与旗标（L2/L3/L4 开关）由调用方注入 features/flags——
    本函数零 LLM、零 IO（落库由调用方在 MTA_ROUTER=1 时执行）。"""
    f = dict(features)
    if language:
        f["language"] = language
    flags = flags or {}
    # 语言执行链路旗标（L2/L3/L4 现有开关语义对齐：路线随链路开关走）
    if not flags.get("l2"):
        f.setdefault("_l2_off", True)
    out = []
    for route in ROUTES:
        # 语言旗标联动：L2/L3/L4 关闭时按既有开关语义剔除（诚实记录）
        if route.id in ("l2_contract_http", "l3_scenario", "l4_acceptance") \
                and not flags.get(route.id.split("_")[0]):
            out.append({"route_id": route.id, "name": route.name, "selected": False,
                        "reason": f"路线随执行链路开关关闭（{route.id.split('_')[0]} 旗标 off）",
                        "priority": route.priority, "oracle_type": route.oracle_type,
                        "cost": route.cost})
            continue
        ok, why = evaluate_route(route, f)
        out.append({"route_id": route.id, "name": route.name, "selected": ok,
                    "reason": why, "priority": route.priority,
                    "oracle_type": route.oracle_type, "cost": route.cost})
    sel = sorted([r for r in out if r["selected"]], key=lambda r: r["priority"])
    result = {"routes": out, "selected_ids": [r["route_id"] for r in sel]}
    if conn is not None and task_id is not None:
        save_route_plan(conn, task_id, out)
    return result


def save_route_plan(conn, task_id: int, routes: list[dict]) -> None:
    """落 route_plan 表（幂等：同任务清后重写）。每条路线一行，含被剔除者（可审计）。"""
    conn.execute("DELETE FROM route_plan WHERE task_id=?", (task_id,))
    for r in routes:
        conn.execute(
            """INSERT INTO route_plan(task_id, route_id, selected, reason, priority)
               VALUES (?,?,?,?,?)""",
            (task_id, r["route_id"], 1 if r["selected"] else 0,
             r["reason"][:500], r["priority"]))
    conn.commit()
