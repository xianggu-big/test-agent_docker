"""L2 白盒轨：AST 调用图（确定性，零 LLM 零网络）——蓝图 §3.2「调用链白盒」P1 版。

三件事：
1. 调用边收集：被测源码（非测试 .py）AST 解析，收集 函数→函数 调用边
   （同模块直呼 + from-import 跨模块 + module.attr 跨模块，方法调用记 Class.method）。
2. 端点→函数映射：FastAPI 导出的 operationId 默认形如 "<handler>_<path>_<method>"，
   取下划线首段反查函数名（确定性启发式，映射不上如实留空不硬猜）。
3. 顺序约束场景：同资源前缀下 POST（创建）→ GET /res/{param}（按 id 读）配对，
   生成"先创建后查询"场景（创建响应取 id → 查询断言 200 且字段一致）。

落库：call_edges（caller = 'route:METHOD /path' 或 'module.func'，depth = 距 handler 深度）。
"""
import ast
from pathlib import Path

from .. import config

_MAX_DEPTH = 3
_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _calls_of(fn: ast.FunctionDef) -> list[str]:
    """函数体内的调用名：Name() 直呼 / mod.fn() 属性调用 / self.method()。"""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name):
            out.append(f.id)
        elif isinstance(f, ast.Attribute):
            out.append(f.attr)          # self.method / module.func 都记末端名（解析阶段消歧）
    return out


def build_call_edges(ws: Path) -> dict[str, list[str]]:
    """模块级调用图：{caller_qualname: [callee_qualname...]}。caller/callee 用 'module.func'。"""
    ws = Path(ws)
    funcs: dict[str, ast.FunctionDef] = {}          # qualname -> 节点
    raw_edges: list[tuple[str, str]] = []           # (caller_qualname, 调用名)
    for p in sorted(ws.rglob("*.py")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        mod = p.stem
        for name, fn in _module_functions(tree).items():
            qual = f"{mod}.{name}"
            funcs[qual] = fn
            for c in _calls_of(fn):
                raw_edges.append((qual, c))
    # 消歧：调用名 → 已知函数（优先同模块，其次任意模块唯一匹配）
    by_module: dict[str, set[str]] = {}
    for qual in funcs:
        mod, _, fname = qual.rpartition(".")
        by_module.setdefault(mod, set()).add(fname)
    edges: dict[str, set[str]] = {}
    for caller, callee in raw_edges:
        mod = caller.rpartition(".")[0]
        target = None
        if f"{mod}.{callee}" in funcs:
            target = f"{mod}.{callee}"
        else:
            hits = [q for q in funcs if q.rpartition(".")[2] == callee]
            if len(hits) == 1:
                target = hits[0]
        if target and target != caller:
            edges.setdefault(caller, set()).add(target)
    return {k: sorted(v) for k, v in edges.items()}


def handler_name(endpoint: dict, func_tails: set[str]) -> str | None:
    """operationId 反查函数名：FastAPI 约定 '<handler>_<path>_<method>'，但 handler 本身可含
    下划线（create_coupon_coupons_post）——按「最长函数名前缀+下划线」匹配（确定性，
    多个同级匹配或无匹配 → None，不硬猜）。"""
    op = endpoint.get("operation_id") or ""
    if not op:
        return None
    hits = sorted((t for t in func_tails if op.startswith(t + "_")), key=len, reverse=True)
    return hits[0] if hits else None


def handler_of(endpoint: dict, funcs) -> str | None:
    """operationId 反查 handler 全名（funcs: {qualname: ...} 或 set[qualname]）。"""
    tails = {q.rpartition(".")[2] for q in funcs}
    name = handler_name(endpoint, tails)
    if name is None:
        return None
    matches = [q for q in funcs if q.rpartition(".")[2] == name]
    return matches[0] if len(matches) == 1 else None


def handler_edges(endpoints: list[dict], call_graph: dict[str, list[str]]
                  ) -> list[tuple[str, str, int]]:
    """端点为根的调用边展平：[(caller, callee, depth)]，caller='route:METHOD /path'。
    handler 是调用图的根（无人调用它），匹配域 = caller ∪ callee 全体函数名。"""
    all_funcs = set(call_graph) | {c for cs in call_graph.values() for c in cs}
    tails = {q.rpartition(".")[2] for q in all_funcs}
    out: list[tuple[str, str, int]] = []
    for ep in endpoints:
        route = f"route:{ep['method']} {ep['path']}"
        name = handler_name(ep, tails)
        roots = sorted(q for q in all_funcs if q.rpartition(".")[2] == name) if name else []
        seen: set[str] = set()
        frontier = list(roots)
        depth = 1
        while frontier and depth <= _MAX_DEPTH + 1:
            nxt: list[str] = []
            for node in frontier:
                if node in seen:
                    continue
                seen.add(node)
                out.append((route, node, depth))
                for callee in call_graph.get(node, []):
                    if callee not in seen:
                        nxt.append(callee)
                        out.append((node, callee, depth + 1))
            frontier = nxt
            depth += 1
    return out


def sequence_scenarios(endpoints: list[dict]) -> list[dict]:
    """顺序约束场景（v0 启发式）：同资源前缀下 POST /res（创建）→ GET /res/{param}（按 id 读）
    配对一条场景；返回 [{create: endpoint, read: endpoint, id_param: 名称}]。"""
    by_route = {(e["method"], e["path"]): e for e in endpoints}
    scenes = []
    for (method, path), ep in by_route.items():
        if method != "POST":
            continue
        res = "/" + path.strip("/").split("/")[0] if path.strip("/") else ""
        if not res:
            continue
        for (m2, p2), ep2 in by_route.items():
            if m2 != "GET" or not p2.startswith(res + "/"):
                continue
            tail = p2[len(res):].strip("/")
            segs = [s for s in tail.split("/") if s]
            if len(segs) == 1 and segs[0].startswith("{") and segs[0].endswith("}"):
                scenes.append({"create": ep, "read": ep2,
                               "id_param": segs[0][1:-1]})
    return scenes
