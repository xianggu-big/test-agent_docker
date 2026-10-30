"""四级 RTM 跨级溯源链 v1（P2 跨级链，E7 前置）。

目标：高层失败沿「验收场景 → 系统场景 → 集成用例 → 单元用例 → 代码位置」反向定位。
纪律（与全项目同源）：
- 纯确定性：只读 SQL + AST 落库快照，零 LLM、零网络；映射不硬猜，缺环如实进 gaps。
- 不冒充逐用例执行结果：L2/L1 用例没有逐条结果台账（只有轮次级 passed/failed），
  链接节点记 status='candidate' 并带 link_basis；触发场景本身的失败状态才是证据。
- 代码位置=函数定义级（call_edges.file/lineno），不是精确缺陷行；E7 命中口径按
  file::func 比对，论文如实写"函数级定位"。

链接依据（按优先级）：
1) 场景步骤端点 → 接口模板归一 → L2 用例（test_cases.steps 记 'METHOD /path'）；
2) 场景文本/requirement_ref × 测试条件 2-gram → rtm_maps.cases → L1 用例；
3) call_edges：route 行起点 → 按 caller/callee 走调用树 → 文件:行号；
4) L4 上游：同端点/同需求文本的已确认验收场景（仅 L3 触发时）。
"""
import json
import re

from .evaluate import _grams

_ROUTE_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH)\s+(\S+)")
_FAILED_STEP = ("pending_confirm", "confirmed_defect")
_TRACE_STATUSES = ("pending_confirm", "confirmed_defect")


def _json(raw, default):
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def route_key(method: str, path: str, endpoints: list[dict]) -> str:
    """场景步骤 → 接口模板 route key（'METHOD /path'）。

    先精确匹配；再把带 `{param}` 的端点模板转正则，把真实 id（/coupons/1）归一到
    /coupons/{cid}；都不中则原样返回（不硬猜成别的端点）。
    """
    method = (method or "").upper()
    path = path or ""
    for e in endpoints:
        if e["method"].upper() == method and e["path"] == path:
            return f"{method} {e['path']}"
    for e in endpoints:
        if e["method"].upper() != method or "{" not in e["path"]:
            continue
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", e["path"])
        if re.fullmatch(pattern, path):
            return f"{method} {e['path']}"
    return f"{method} {path}"


def script_level(framework: str | None, content: str | None) -> str:
    """测试脚本所属级别（确定性启发式，供跨级链区分 L1/L2/L4）。

    acceptance → L4；requests/BASE=http 的 pytest → L2；其余 pytest（TestClient/纯函数）→ L1。
    仅用于溯源展示，不写回 test_scripts（级别真源仍是 test_levels 台账）。
    """
    if (framework or "") == "acceptance":
        return "L4"
    c = content or ""
    if "requests" in c or re.search(r"BASE\s*=\s*['\"]http", c):
        return "L2"
    return "L1"


def _scenario_text(sc: dict) -> str:
    gwt = _json(sc.get("gwt"), {}) or {}
    parts = [sc.get("title") or "", sc.get("expected") or "",
             sc.get("requirement_ref") or "", gwt.get("given") or "",
             gwt.get("when") or "", gwt.get("then") or ""]
    return " ".join(p for p in parts if p)


def _overlap(query: str, target: str) -> float:
    qg = _grams(query)
    if not qg:
        return 0.0
    tg = _grams(target)
    return len(qg & tg) / len(qg)


def _failed_step(sc: dict) -> dict | None:
    last = _json(sc.get("last_result"), {})
    for st in last.get("steps") or []:
        if not st.get("ok"):
            return st
    return None


def _scenario_routes(sc: dict, endpoints: list[dict]) -> list[str]:
    out = []
    for st in _json(sc.get("steps"), []) or []:
        key = route_key(st.get("method"), st.get("path"), endpoints)
        if key not in out:
            out.append(key)
    return out


def _script_rows(conn, task_id: int) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, framework, content FROM test_scripts WHERE task_id=?",
        (task_id,))}


def _cases(conn, task_id: int, scripts: dict[int, dict]) -> list[dict]:
    rows = conn.execute(
        """SELECT c.* FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
           WHERE s.task_id=? AND c.enabled=1 ORDER BY c.id""", (task_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        s = scripts.get(d["script_id"]) or {}
        d["level"] = script_level(s.get("framework"), s.get("content"))
        out.append(d)
    return out


def _l4_upstream(conn, task_id: int, target: dict, focus_route: str | None,
                 endpoints: list[dict]) -> list[dict]:
    """已确认 L4 验收场景中，与触发场景「失败步骤端点」相同或高文本重叠的上游节点。

    注意：不能用场景全部步骤端点做交集——创建步骤是所有下单场景的公共前缀，
    会把无关验收场景拉进链（单测实锤后收紧为失败步骤端点）。
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT id,title,requirement_ref,steps,expected,gwt,status
           FROM scenario_cases WHERE task_id=? AND level='L4' AND status IN
           ('confirmed_pass','confirmed_defect','confirmed_scenario') ORDER BY id""",
        (task_id,))]
    text = _scenario_text(target)
    scored = []
    for sc in rows:
        routes = _scenario_routes(sc, endpoints)
        inter = 1 if focus_route and focus_route in routes else 0
        score = _overlap(text, _scenario_text(sc))
        # 仅同端点不够（创建步骤是所有下单场景的公共前缀）；须有文本证据，
        # 或同端点且有弱文本证据。
        if score >= 0.35 or (inter and score >= 0.2):
            scored.append((inter, score, sc))
    scored.sort(key=lambda x: (-x[1], -x[0], x[2]["id"]))
    out = []
    for inter, score, sc in scored[:2]:
        out.append({"level": "L4", "kind": "acceptance_scenario", "id": sc["id"],
                    "title": sc["title"], "status": sc["status"],
                    "detail": f"同端点 {inter} 个 / 文本重叠 {score:.2f}（上游验收场景）",
                    "link_basis": "endpoint+text" if inter else "text"})
    return out


def _l2_nodes(cases: list[dict], target_routes: list[str], focus: str | None,
              text: str) -> list[dict]:
    scored = []
    for c in cases:
        if c["level"] != "L2":
            continue
        m = _ROUTE_RE.match((c.get("steps") or "").strip())
        route = f"{m.group(1).upper()} {m.group(2)}" if m else None
        if route not in target_routes:
            continue
        s = _overlap(text, f"{c.get('name') or ''} {c.get('expected') or ''}")
        scored.append((1 if route == focus else 0, s, c))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["id"]))
    out = []
    for is_focus, s, c in scored[:5]:
        out.append({"level": "L2", "kind": "integration_case", "id": c["id"],
                    "name": c["name"], "status": "candidate",
                    "route": c.get("steps"), "method": c.get("method"),
                    "expected": c.get("expected"),
                    "expected_basis": c.get("expected_basis"),
                    "detail": f"端点关联{'（失败步骤）' if is_focus else ''} / 文本重叠 {s:.2f}"
                              "（候选，无逐用例结果台账）",
                    "link_basis": "endpoint"})
    return out


def _pick_condition(conn, task_id: int, text: str) -> tuple[dict | None, list[str], str]:
    """场景文本 → 测试条件 → rtm_maps.cases。返回 (condition, case_names, basis)。"""
    matrix = conn.execute("SELECT matrix FROM rtm_maps WHERE task_id=?"
                          " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    conditions = [dict(r) for r in conn.execute(
        "SELECT id, requirement_ref, condition, priority FROM test_conditions"
        " WHERE task_id=? ORDER BY id", (task_id,))]
    if matrix and conditions:
        rows = _json(matrix["matrix"], []) or []
        best, best_score = None, 0.0
        for m in rows:
            ctext = f"{m.get('requirement_ref') or ''} {m.get('condition') or ''}"
            s = _overlap(text, ctext)
            if s > best_score:
                best, best_score = m, s
        if best and best_score >= 0.25 and best.get("cases"):
            return ({"id": best.get("condition_id"), "condition": best.get("condition"),
                     "requirement_ref": best.get("requirement_ref"),
                     "priority": best.get("priority")}, list(best["cases"]),
                    f"rtm 2-gram {best_score:.2f}")
    if conditions:
        best, best_score = None, 0.0
        for c in conditions:
            s = _overlap(text, f"{c['requirement_ref'] or ''} {c['condition']}")
            if s > best_score:
                best, best_score = c, s
        if best and best_score >= 0.25:
            return best, [], f"condition 2-gram {best_score:.2f}（rtm 无映射）"
    return None, [], ""


def _l1_nodes(cases: list[dict], conn, task_id: int, text: str) -> tuple[list[dict], str]:
    cond, names, basis = _pick_condition(conn, task_id, text)
    l1 = [c for c in cases if c["level"] == "L1"]
    picked = [c for c in l1 if c["name"] in set(names)] if names else []
    if not picked:
        # RTM 无映射时按用例文本直接匹配（仍是确定性 2-gram）
        scored = [(_overlap(text, f"{c['name']} {c.get('expected') or ''} {c.get('content') or ''}"),
                   c) for c in l1]
        scored = [(s, c) for s, c in scored if s >= 0.3]
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        picked = [c for _, c in scored[:3]]
        basis = (basis + "；" if basis else "") + "case 2-gram"
    out = []
    for c in picked[:5]:
        out.append({"level": "L1", "kind": "unit_case", "id": c["id"],
                    "name": c["name"], "status": "candidate",
                    "expected": c.get("expected"),
                    "expected_basis": c.get("expected_basis"),
                    "condition": (cond or {}).get("condition"),
                    "detail": f"需求条件关联（{basis or '无映射'}；候选，无逐用例结果台账）",
                    "link_basis": basis or "none"})
    return out, basis


def _code_nodes(conn, task_id: int, focus_routes: list[str]) -> tuple[list[dict], list[str]]:
    edges = [dict(r) for r in conn.execute(
        """SELECT caller, callee, depth, file, lineno FROM call_edges
           WHERE task_id=? ORDER BY id""", (task_id,))]
    if not edges:
        return [], ["无调用图快照（L2 未产出 call_edges / 老任务）——代码位置缺席"]
    adj: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for e in edges:
        if e["caller"].startswith("route:"):
            roots.append(e)
        else:
            adj.setdefault(e["caller"], []).append(e)
    wanted = {f"route:{r}" for r in focus_routes}
    roots = [r for r in roots if r["caller"] in wanted] or roots
    out, seen = [], set()
    for root in roots:
        stack = [root]
        while stack and len(out) < 8:
            e = stack.pop(0)
            key = (e["callee"], e.get("file"), e.get("lineno"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"level": "CODE", "kind": "code_location",
                        "function": e["callee"], "file": e.get("file"),
                        "lineno": e.get("lineno"), "depth": e["depth"],
                        "role": "handler" if e["caller"].startswith("route:") else "callee",
                        "detail": f"{e['caller']} → {e['callee']}（深度 {e['depth']}）"})
            stack.extend(adj.get(e["callee"], []))
    gaps = [] if out else ["调用图无该端点 handler 映射——代码位置缺席"]
    return out, gaps


def build_trace_chain(conn, task_id: int, scenario_id: int) -> dict:
    """单场景跨级链（确定性）。scenario_id 不属于该任务 → LookupError（API 404）。"""
    sc = conn.execute("SELECT * FROM scenario_cases WHERE id=?", (scenario_id,)).fetchone()
    if not sc or sc["task_id"] != task_id:
        raise LookupError(f"场景 {scenario_id} 不属于任务 {task_id}")
    sc = dict(sc)
    endpoints = [dict(r) for r in conn.execute(
        "SELECT method, path FROM interface_endpoints WHERE task_id=? ORDER BY id",
        (task_id,))]
    routes = _scenario_routes(sc, endpoints)
    failed = _failed_step(sc)
    focus = None
    if failed:
        m = _ROUTE_RE.match(failed.get("step") or "")
        focus = route_key(m.group(1), m.group(2), endpoints) if m else None
    focus_routes = [focus] if focus else routes

    scripts = _script_rows(conn, task_id)
    cases = _cases(conn, task_id, scripts)
    text = _scenario_text(sc)

    nodes: list[dict] = []
    if sc["level"] == "L3":
        nodes += _l4_upstream(conn, task_id, sc, focus, endpoints)
    nodes.append({"level": sc["level"], "kind": "system_scenario" if sc["level"] == "L3"
                  else "acceptance_scenario", "id": sc["id"], "title": sc["title"],
                  "status": sc["status"], "requirement_ref": sc.get("requirement_ref"),
                  "expected": sc.get("expected"),
                  "detail": (f"失败步骤 {failed.get('step')}（实际 {failed.get('status')}）"
                             if failed else "无冒烟失败记录"),
                  "link_basis": "trigger"})
    nodes += _l2_nodes(cases, routes, focus, text)
    l1_nodes, l1_basis = _l1_nodes(cases, conn, task_id, text)
    nodes += l1_nodes
    code, gaps = _code_nodes(conn, task_id, focus_routes)
    if not l1_nodes:
        gaps.append("无 L1 单元用例关联（RTM 无映射/文本不匹配）")
    nodes += code

    levels = [dict(r) for r in conn.execute(
        "SELECT id, level, status, verdict, trace_from, summary FROM test_levels"
        " WHERE task_id=? ORDER BY id", (task_id,))]
    return {"task_id": task_id, "scenario_id": scenario_id,
            "trigger": {"level": sc["level"], "title": sc["title"], "status": sc["status"],
                        "requirement_ref": sc.get("requirement_ref"),
                        "focus_route": focus, "routes": routes},
            "nodes": nodes, "gaps": gaps,
            "level_chain": levels, "l1_link_basis": l1_basis}


def build_task_traces(conn, task_id: int) -> list[dict]:
    """任务级：对需要溯源的失败/待定性场景逐条建链（auto_pass/pending 不触发）。"""
    rows = conn.execute(
        """SELECT id FROM scenario_cases WHERE task_id=? AND status IN (?,?)
           ORDER BY level, id""", (task_id, *_TRACE_STATUSES)).fetchall()
    return [build_trace_chain(conn, task_id, r["id"]) for r in rows]
