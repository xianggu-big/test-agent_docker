"""L2 集成层子图（四级双轨 P1 主战场，蓝图 §3.2/§3.5/§11-D4/D7/D9）。

    extract → callgraph → provision(依赖+装 requests/jsonschema+起服务+健康检查)
            → generate(契约/顺序/需求三轨) → run(pytest test_api_r1.py) → evaluate → report

编排口径（D9）：L2 不嵌进 L1 的 LangGraph 图——由 levels.level_supervisor 串行接力，
本级是独立 StateGraph，图结构与 L1 同款节点形态（固定图做骨架，LLM 产物进节点）。

v0 沙箱（§3.5 降级路径）：单容器——被测服务与测试执行同容器 localhost 互通，
双容器网络模型（docker network）后置；容器随 leg 结束回收（服务随之终止）。

诚实纪律（与 L1 同源）：D4 无 spec → leg 如实 skipped；契约断言失败=契约违背（先归因，
可能为实现缺陷或契约漂移）；需求断言失败=疑似需求级缺陷（证人语义，不回炉迁就）；
无 LLM Key → 需求轨 fail-open 缺席，契约轨照跑。
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import TypedDict

from ..skills.llm_client import make_chat

from .. import config
from ..skills.callgraph import (build_call_edges, collect_functions,
                                route_call_edges, sequence_scenarios)
from ..skills.contract_gen import build_requirement_asserts
from ..skills.endpoint_extract import probe_endpoint_source
from ..skills.registry import Skill
from ..store.db import get_conn

log = logging.getLogger(__name__)


class L2State(TypedDict, total=False):
    task_id: int
    session_id: int
    workspace: str
    container_name: str
    requirement: str
    # 链路产物
    endpoint_source: str          # spec:openapi.json / no_spec（D4 降级链留痕）
    endpoints: list[dict]
    scenarios: list[dict]         # 顺序场景（白盒联动）
    skipped_routes: list[str]     # 无确定 id 未生成用例的端点（如实上报）
    service_port: int
    service_ready: bool
    # 执行与判定
    last_exit: int
    last_stdout: str
    passed: int
    failed: int
    contract_violations: int
    requirement_failures: int
    verdict: str                  # pass / stop / skipped
    stop_reason: str
    done: bool


# ---------------------------------------------------------------------------
# L2 Skill 层（func(task_id, args) -> str；线程池执行——连接 func 内自建，老坑纪律）
# ---------------------------------------------------------------------------

def build_l2_skills(runner) -> dict[str, Skill]:
    """L2 三个 skill：端点提取落库 / 调用图落库 / 契约用例生成回灌——全走注册表留审计。"""

    def extract_endpoints(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            eps, how = probe_endpoint_source(Path(args["workspace"]))
            conn.execute("DELETE FROM interface_endpoints WHERE task_id=?", (task_id,))
            for e in eps or []:
                conn.execute(
                    """INSERT INTO interface_endpoints(task_id, method, path, operation_id,
                           params, request_schema, response_schema, source)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (task_id, e["method"], e["path"], e.get("operation_id"),
                     json.dumps(e.get("params"), ensure_ascii=False),
                     json.dumps(e.get("request_schema"), ensure_ascii=False),
                     json.dumps(e.get("response_schema"), ensure_ascii=False),
                     e.get("source", "spec")))
            conn.commit()
            return json.dumps({"endpoints": eps, "how": how}, ensure_ascii=False)
        finally:
            conn.close()

    def build_callgraph(task_id: int, args: dict) -> str:
        conn = get_conn()
        try:
            ws = Path(args["workspace"])
            graph = build_call_edges(ws)
            eps = args.get("endpoints") or []
            edges = route_call_edges(eps, graph, collect_functions(ws))
            scenes = sequence_scenarios(eps)
            conn.execute("DELETE FROM call_edges WHERE task_id=?", (task_id,))
            for e in edges:
                conn.execute(
                    """INSERT INTO call_edges(task_id, caller, callee, depth, file, lineno)
                       VALUES (?,?,?,?,?,?)""",
                    (task_id, e["caller"], e["callee"], e["depth"],
                     e.get("file"), e.get("lineno")))
            conn.commit()
            return json.dumps({"edge_count": len(edges), "scenarios": scenes},
                              ensure_ascii=False)
        finally:
            conn.close()

    def gen_contract_tests(task_id: int, args: dict) -> str:
        from ..skills import contract_gen as cg
        conn = get_conn()
        try:
            ws = Path(args["workspace"])
            if args.get("endpoints"):
                endpoints = [dict(e) for e in args["endpoints"]]   # state 直传（字段全）
            else:
                rows = conn.execute(
                    "SELECT method, path, operation_id, params, request_schema,"
                    " response_schema, source FROM interface_endpoints"
                    " WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
                endpoints = [{"method": r["method"], "path": r["path"],
                              "operation_id": r["operation_id"],
                              "params": json.loads(r["params"] or "[]"),
                              "request_schema": json.loads(r["request_schema"])
                              if r["request_schema"] else None,
                              "response_schema": json.loads(r["response_schema"])
                              if r["response_schema"] else None,
                              "source": r["source"], "success_code": 200,
                              "documented_codes": []} for r in rows]

            if not config.L2_BLACKBOX_ENABLED:   # E6 L2E1：无黑盒轨（契约/需求用例跳过）
                contract_code, cases, happy, skipped = "", [], {}, []
            else:
                contract_code, cases, happy, skipped = cg.render_contract_module(
                    endpoints, f"http://127.0.0.1:{args.get('service_port', config.L2_SERVICE_PORT)}")
            seq_code, seq_cases = cg.render_sequence_module(
                args.get("scenarios") or [], happy,
                f"http://127.0.0.1:{args.get('service_port', config.L2_SERVICE_PORT)}",
                endpoints=endpoints)
            req_code, req_cases = "", []
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            requirement = args.get("requirement") or ""
            if api_key and requirement and config.L2_BLACKBOX_ENABLED:
                llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=api_key,
                                 base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                                 timeout=60, max_retries=2)
                body_eps = [e for e in endpoints
                            if f"{e['method']} {e['path']}" in happy][:2]
                asserts = cg.build_requirement_asserts(requirement, body_eps, happy, llm, task_id)
                for route_key, alist in asserts.items():
                    ep = next((e for e in body_eps
                               if f"{e['method']} {e['path']}" == route_key), None)
                    if ep:
                        rc, rcs = cg.render_requirement_module(
                            alist, ep, happy.get(route_key),
                            f"http://127.0.0.1:{args.get('service_port', config.L2_SERVICE_PORT)}",
                            endpoints=endpoints, happy=happy)
                        req_code += rc
                        req_cases += rcs

            content = contract_code + "\n" + seq_code + "\n" + req_code
            fname = "test_api_r1.py"
            (ws / fname).write_text(content, encoding="utf-8")
            cur = conn.execute(
                """INSERT INTO test_scripts(task_id, module_id, framework, version, content,
                                            content_hash) VALUES (?, ?, 'pytest', 1, ?, '')""",
                (task_id, args.get("module_id"), content))
            script_id = cur.lastrowid
            for c in cases + seq_cases + req_cases:
                conn.execute(
                    """INSERT INTO test_cases(script_id, source, name, method, content,
                                              input, steps, expected, expected_basis)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (script_id, "ai", c["name"], c["method"], "",
                     json.dumps(c.get("input"), ensure_ascii=False),
                     c.get("route"), c.get("expected"), c.get("expected_basis")))
            conn.commit()
            container = runner.client.containers.get(args["container_name"])
            runner.copy_dir_into(container, ws)
            return json.dumps({"contract": len(cases), "sequence": len(seq_cases),
                               "requirement": len(req_cases), "file": fname,
                               "skipped_routes": skipped}, ensure_ascii=False)
        finally:
            conn.close()

    return {
        "extract_endpoints": Skill(
            name="extract_endpoints", description="D4 降级链：OpenAPI spec 提取端点清单（诚实降级）",
            func=extract_endpoints, risk_level="low", timeout_ms=60_000,
            arg_schema={"workspace": "str"}),
        "build_callgraph": Skill(
            name="build_callgraph", description="L2 白盒轨：AST 调用图+端点调用边+顺序场景",
            func=build_callgraph, risk_level="low", timeout_ms=60_000,
            arg_schema={"workspace": "str"}),
        "gen_contract_tests": Skill(
            name="gen_contract_tests", description="契约黑盒三轨生成（契约/顺序/需求）并回灌容器",
            func=gen_contract_tests, risk_level="medium", timeout_ms=180_000,
            arg_schema={"workspace": "str", "container_name": "str"}),
    }


# ---------------------------------------------------------------------------
# v0 单容器服务管理（§3.5 降级路径）：起服务 + 健康检查，容器随 leg 回收
# ---------------------------------------------------------------------------

def _detect_app_module(ws: Path) -> str | None:
    """确定性地找 FastAPI 应用模块：根目录 .py 里含 'FastAPI(' 的第一个文件名 stem。"""
    ws = Path(ws)
    for p in sorted(ws.glob("*.py")):
        try:
            if "FastAPI(" in p.read_text(encoding="utf-8", errors="ignore"):
                return p.stem
        except OSError:
            continue
    return None


def _start_service(runner, container, app_module: str, port: int) -> tuple[bool, str]:
    # P2-6（2026-10-31 体检）：app_module 拼进 sh -c，文件名含 shell 元字符=命令注入；
    # 模块名必须是合法 Python 标识符（点号分层允许），否则拒绝起服务（fail-closed）
    import re as _re
    if not _re.fullmatch(r"[A-Za-z_][\w.]*(\.[A-Za-z_]\w*)*", app_module or ""):
        return False, f"app_module 非法（非标识符）：{app_module[:60]}"
    """容器内后台起 uvicorn 并轮询健康（/openapi.json）；返回 (就绪?, 失败说明)。"""
    runner.exec_in(container, ["sh", "-c",
                   f"cd /work && nohup python -m uvicorn {app_module}:app "
                   f"--host 127.0.0.1 --port {port} > /tmp/l2svc.log 2>&1 &"],
                   timeout_s=15)
    wait = int(config.L2_SERVICE_WAIT_S)
    chk = (
        "import time,urllib.request,sys\n"
        f"deadline=time.time()+{wait}\n"
        "while True:\n"
        "    try:\n"
        f"        urllib.request.urlopen('http://127.0.0.1:{port}/openapi.json', timeout=2)\n"
        "        print('READY'); sys.exit(0)\n"
        "    except Exception:\n"
        "        if time.time()>deadline:\n"
        "            print('NOT_READY'); sys.exit(1)\n"
        "        time.sleep(1)\n")
    r = runner.exec_in(container, ["python", "-c", chk], timeout_s=wait + 15)
    if r.exit_code == 0 and "READY" in r.stdout:
        return True, ""
    tail = runner.exec_in(container, ["sh", "-c", "tail -n 20 /tmp/l2svc.log"],
                          timeout_s=10)
    return False, (tail.stdout or tail.stderr or "服务未就绪")[-300:]


def judge_l2(stdout: str, last_exit: int, endpoints: list, skipped_routes: list) -> tuple[dict, str, str]:
    """L2 判读纯函数（Loop-1 自 run() 内联抽出，语义除 P2-5a 修复外逐字节不变）。

    P2-5a（2026-10-31 体检）：pytest exit=5（收集失败）且无 FAILED 行时，旧逻辑落
    『契约违背 0 处：实现与 spec 不符』——0 处违背却声称实现不符，语义误导。
    现在：failed==0 且 exit!=0 → 『无可执行用例』诚实降级语义（不判契约）。"""
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", stdout)) else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", stdout)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) errors?", stdout)) else 0
    failed += errors
    from ..graph.repair import failing_nodeids
    nodeids = failing_nodeids(stdout)
    req_fail = sum(1 for n in nodeids if "需求断言" in n)
    contract_fail = failed - req_fail
    eps = endpoints or []
    skipped_n = len(skipped_routes or [])
    summary = {"passed": passed, "failed": failed,
               "requirement_failures": req_fail, "contract_violations": contract_fail,
               "endpoints_total": len(eps),
               # TODO-07（A 直接改名，2026-11-06 用户拍板）：旧名 endpoints_covered 语义
               # 实为「未跳过」并不等于「断言真实命中」，与覆盖表 hit 术语对齐；
               # 全量改名（生产/消息/前端/单测），旧名零残留
               "endpoints_hit": max(0, len(eps) - skipped_n)}
    if last_exit == 0 and failed == 0:
        verdict, reason = "pass", "契约与需求断言全过（L2 单轮 v0，无 R-Loop）"
    elif failed == 0:
        verdict, reason = ("stop", f"无可执行用例（pytest exit={last_exit}，收集/环境失败）："
                                   f"容器内测试未真正运行，无法判定契约语义（诚实降级）")
    elif req_fail:
        verdict, reason = ("stop", f"疑似需求级缺陷：需求断言失败 {req_fail} 条保留为证人"
                                   f"（不回炉迁就，人工定性）")
    else:
        verdict, reason = ("stop", f"契约违背 {contract_fail} 处：实现与 spec 不符"
                                   f"（可能为实现缺陷或契约漂移，先归因需人工定性）")
    return summary, verdict, reason


# ---------------------------------------------------------------------------
# L2 子图节点
# ---------------------------------------------------------------------------

def make_l2_nodes(deps):
    conn, registry, skills = deps.conn, deps.registry, deps.skills

    def msg(session_id: int, content: str) -> None:
        seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                           (session_id,)).fetchone()["n"]
        conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
                     (session_id, seq, "assistant", content))
        conn.commit()

    def _leg_update(task_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE task_id=? AND level='L2'",
                     (*fields.values(), task_id))
        conn.commit()

    def extract(state: L2State) -> dict:
        tid = state["task_id"]
        res = registry.invoke(tid, skills["extract_endpoints"],
                              {"workspace": state["workspace"]})
        data = json.loads(res.output or "{}")
        eps, how = data.get("endpoints"), data.get("how", "no_spec")
        if not eps:
            msg(state["session_id"], f"[L2·extract] 无可用端点提取路径（{how}）："
                                     f"诚实降级，本级 skipped（D4）")
            _leg_update(tid, strategy="degraded", status="skipped",
                        verdict="skipped", stop_reason=f"无端点提取路径（{how}）",
                        finished_at=_now())
            return {"done": True, "verdict": "skipped", "stop_reason": f"无端点提取路径（{how}）",
                    "endpoint_source": how}
        msg(state["session_id"], f"[L2·extract] {len(eps)} 个端点（来源 {how}）落库")
        return {"endpoints": eps, "endpoint_source": how}

    def callgraph(state: L2State) -> dict:
        tid = state["task_id"]
        if not config.L2_WHITEBOX_ENABLED:   # E6 L2E2：无白盒轨（诚实留痕，不冒充）
            msg(state["session_id"], "[L2·callgraph] 白盒轨开关关闭：跳过调用图与顺序场景（消融格）")
            return {"scenarios": []}
        res = registry.invoke(tid, skills["build_callgraph"],
                              {"workspace": state["workspace"],
                               "endpoints": state.get("endpoints") or []})
        data = json.loads(res.output or "{}")
        scenes = data.get("scenarios") or []
        msg(state["session_id"], f"[L2·callgraph] 调用边 {data.get('edge_count', 0)} 条落库，"
                                 f"顺序场景 {len(scenes)} 个")
        return {"scenarios": scenes}

    def provision(state: L2State) -> dict:
        tid = state["task_id"]
        runner = deps.runner
        container = runner.ensure_container(config.IMAGE_PY, state["container_name"], fresh=True)
        runner.copy_dir_into(container, state["workspace"])
        deps.registry.invoke(tid, deps.skills["install_deps"], {
            "container_name": state["container_name"],
            "has_requirements": (Path(state["workspace"]) / "requirements.txt").exists()})
        # 测试执行器依赖（v0 单容器：requests+jsonschema 随开关装，同 hypothesis 先例）
        runner.exec_in(container, ["pip", "install", "-q", "requests", "jsonschema"],
                       timeout_s=300)
        app_module = _detect_app_module(Path(state["workspace"]))
        if not app_module:
            _leg_update(tid, status="failed", verdict="stop",
                        stop_reason="未找到 FastAPI 应用模块（app.py/main.py 含 FastAPI( ）",
                        finished_at=_now())
            msg(state["session_id"], "[L2·provision] 未找到 FastAPI 应用模块：本级如实终止")
            return {"done": True, "verdict": "stop", "service_ready": False,
                    "stop_reason": "未找到 FastAPI 应用模块"}
        port = int(config.L2_SERVICE_PORT)
        ok, why = _start_service(runner, container, app_module, port)
        if not ok:
            _leg_update(tid, status="failed", verdict="stop",
                        stop_reason=f"服务启动失败：{why}",
                        finished_at=_now())
            msg(state["session_id"], f"[L2·provision] 服务启动失败：{why}")
            return {"done": True, "verdict": "stop", "service_ready": False,
                    "stop_reason": f"服务启动失败：{why}"}
        msg(state["session_id"], f"[L2·provision] 服务 {app_module}:app 就绪（127.0.0.1:{port}，v0 单容器）")
        return {"service_port": port, "service_ready": True}

    def generate(state: L2State) -> dict:
        tid = state["task_id"]
        res = registry.invoke(tid, skills["gen_contract_tests"], {
            "workspace": state["workspace"], "container_name": state["container_name"],
            "requirement": state.get("requirement", ""),
            "endpoints": state.get("endpoints") or [],
            "scenarios": state.get("scenarios") or [],
            "service_port": state.get("service_port", config.L2_SERVICE_PORT)})
        data = json.loads(res.output or "{}")
        msg(state["session_id"], f"[L2·generate] 契约 {data.get('contract', 0)} + "
                                 f"顺序 {data.get('sequence', 0)} + 需求 {data.get('requirement', 0)}"
                                 f" 条用例（跳过路由 {len(data.get('skipped_routes', []))} 个）")
        return {"skipped_routes": data.get("skipped_routes", [])}

    def run(state: L2State) -> dict:
        tid = state["task_id"]
        res = registry.invoke(tid, deps.skills["run_tests"],
                              {"container_name": state["container_name"],
                               # C 后继：L2 也落逐用例结果（level='L2'，看板可按级展示）
                               "cmd": ["pytest", "-q", "--tb=short", "-rfE",
                                       f"--junitxml={config.JUNIT_PATH}", "test_api_r1.py"]})
        if res.status == "denied":
            raise RuntimeError("run_tests 被确认门拒绝：主流程不应在未确认时到达此节点")
        if res.status != "ok":
            out = json.dumps({"exit": -1, "stdout": "",
                              "stderr": f"run_tests {res.status}: {res.error}"}, ensure_ascii=False)
        else:
            out = res.output
        r = json.loads(out)
        # 逐用例结果（L2 leg）落库：与 L1 同解析器，失败不炸主链路
        try:
            from ..skills.case_results import parse_junit_xml, store_case_results
            jc = deps.runner.exec_in(
                deps.runner.client.containers.get(state["container_name"]),
                ["cat", config.JUNIT_PATH], timeout_s=30)
            rows = parse_junit_xml(jc.stdout) if jc.ok else []
            if rows:
                store_case_results(deps.conn, tid,
                                   int(state.get("round_no", 1) or 1), rows, level="L2")
        except Exception as e:      # noqa: BLE001
            log.warning("L2 逐用例结果落库失败 task=%s: %s", tid, e)
        return {"last_exit": r["exit"], "last_stdout": r["stdout"], "last_stderr": r["stderr"]}

    def evaluate(state: L2State) -> dict:
        tid = state["task_id"]
        stdout = state.get("last_stdout", "")
        # Loop-1：判读抽纯函数 judge_l2（P2-5a 修复；下方消息行沿用同名局部量）
        summary, verdict, reason = judge_l2(stdout, int(state.get("last_exit", 1) or 1),
                                            state.get("endpoints"), state.get("skipped_routes"))
        passed = summary["passed"]
        failed = summary["failed"]
        req_fail = summary["requirement_failures"]
        contract_fail = summary["contract_violations"]
        eps = state.get("endpoints") or []
        conn.execute("DELETE FROM evaluation_rounds WHERE task_id=? AND level='L2'", (tid,))
        conn.execute(
            """INSERT INTO evaluation_rounds(task_id, level, round_no, passed, failed,
                                             skipped, verdict, failure_kind, stop_reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, "L2", 1, passed, failed, 0, verdict,
             "product_defect" if req_fail else ("contract_violation" if contract_fail else "none"),
             reason))
        conn.commit()
        _leg_update(tid, verdict=verdict, stop_reason=reason, summary=json.dumps(summary, ensure_ascii=False))
        msg(state["session_id"],
            f"[L2·evaluate] 通过{passed} 失败{failed}（需求级 {req_fail}/契约级 {contract_fail}）"
            f" 端点覆盖 {summary['endpoints_hit']}/{summary['endpoints_total']} → {verdict}")
        return {"passed": passed, "failed": failed, "contract_violations": contract_fail,
                "requirement_failures": req_fail, "verdict": verdict, "stop_reason": reason}

    def report(state: L2State) -> dict:
        tid = state["task_id"]
        verdict = state.get("verdict", "stop")
        # 三态终写：pass→done / skipped（D4 诚实降级）→skipped / 其余→failed
        status = "done" if verdict == "pass" else ("skipped" if verdict == "skipped" else "failed")
        _leg_update(tid, status=status, finished_at=_now())
        conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?",
                     (state["session_id"],))
        conn.commit()
        msg(state["session_id"], f"[L2·report] 本级终态 {verdict}｜{state.get('stop_reason', '')}")
        return {"done": True}

    return {"extract": extract, "callgraph": callgraph, "provision": provision,
            "generate": generate, "run": run, "evaluate": evaluate, "report": report}


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(" ", "seconds")


def build_l2_graph(deps):
    nodes = make_l2_nodes(deps)
    from langgraph.graph import END, StateGraph
    g = StateGraph(L2State)
    for name in ("extract", "callgraph", "provision", "generate", "run",
                 "evaluate", "report"):
        g.add_node(name, nodes[name])
    g.set_entry_point("extract")
    g.add_conditional_edges(
        "extract",
        lambda s: "report" if s.get("done") else "callgraph",
        {"report": "report", "callgraph": "callgraph"})
    g.add_edge("callgraph", "provision")
    g.add_conditional_edges(
        "provision",
        lambda s: "report" if s.get("done") else "generate",
        {"report": "report", "generate": "generate"})
    g.add_edge("generate", "run")
    g.add_edge("run", "evaluate")
    g.add_edge("evaluate", "report")
    g.add_edge("report", END)
    return g.compile()


def run_l2_task(task_id: int, trace_from: int | None = None) -> dict:
    """L2 leg 入口：upsert test_levels 行 → 独立 StateGraph → 容器无论成败一律回收。"""
    from .nodes import GraphDeps, make_deps
    deps: GraphDeps = make_deps()
    conn = deps.conn
    task = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        _leg_upsert(conn, task_id, strategy="degraded", status="failed",
                    verdict="stop", stop_reason="无工作区（L1 未跑或已被清理）",
                    trace_from=trace_from)
        return {"verdict": "stop", "stop_reason": "无工作区"}
    _leg_upsert(conn, task_id, status="running", trace_from=trace_from)
    cur = conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
                       (task_id,))
    conn.commit()
    init: L2State = {
        "task_id": task_id, "session_id": cur.lastrowid,
        "workspace": str(ws), "container_name": f"mta-run-{task_id}",
        "requirement": task["requirement_text"] or "",
    }
    graph = build_l2_graph(deps)
    try:
        return graph.invoke(init, {"recursion_limit": 30})
    except Exception as e:
        _leg_update_by_conn(conn, task_id, status="failed",
                            stop_reason=f"{type(e).__name__}: {e}"[:300], finished_at=_now())
        raise
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{task_id}")
        except Exception as e:  # noqa: BLE001
            log.warning("L2 容器回收失败: %s", e)


def _leg_upsert(conn, task_id: int, strategy: str | None = None,
                trace_from: int | None = None, **fields) -> int:
    """test_levels upsert（UNIQUE(task,level)）：重跑以本轮为准。返回 leg id。"""
    row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level='L2'",
                       (task_id,)).fetchone()
    base = {"strategy": strategy or "full", "status": "queued",
            "trace_from": trace_from, "started_at": _now() if fields.get("status") == "running" else None}
    base.update({k: v for k, v in fields.items()})
    if row:
        sets = ", ".join(f"{k}=?" for k in base)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE id=?", (*base.values(), row["id"]))
        return row["id"]
    cols = ", ".join(base)
    cur = conn.execute(
        f"INSERT INTO test_levels(task_id, level, {cols}) VALUES (?, 'L2', {', '.join('?' for _ in base)})",
        (task_id, *base.values()))
    conn.commit()
    return cur.lastrowid


def _leg_update_by_conn(conn, task_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE test_levels SET {sets} WHERE task_id=? AND level='L2'",
                 (*fields.values(), task_id))
    conn.commit()
