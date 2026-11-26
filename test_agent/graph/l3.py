"""L3 系统测试半自动 leg（D5：冒烟自动判定 + 失败人工介入）。

流转：prepare（容器+服务）→ gen（LLM 场景生成，静态闸=步骤必须命中端点清单）
→ smoke（容器内 requests 顺序执行，全过=auto_pass，任一失败=pending_confirm）
→ report（leg 终态；pending_confirm 场景经 API /api/scenarios/{id}/confirm 人工回库）。
"""
import json
import logging
import os
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

from .. import config
from ..skills.llm_usage import log_llm_call
from ..skills.scenario_gen import (apply_smoke_results, build_smoke_script,
                                   parse_scenarios, save_scenarios)
from ..store.db import get_conn
from .l2 import (_detect_app_module, _detect_go_entry, _detect_java_main,
                 _detect_js_entry, _now, _start_service, _start_service_go,
                 _start_service_java, _start_service_js, _start_service_php)

log = logging.getLogger(__name__)


class L3State(TypedDict, total=False):
    task_id: int
    session_id: int
    workspace: str
    container_name: str
    requirement: str
    primary_language: str         # F 卡 #8（2026-11-10）：go→go126+Go 服务启动
    endpoints: list
    scenarios: list
    service_port: int
    done: bool
    verdict: str
    stop_reason: str


def make_l3_nodes(deps):
    conn, registry = deps.conn, deps.registry

    def msg(session_id: int, content: str) -> None:
        from ..store.db import append_agent_message
        append_agent_message(conn, session_id, "assistant", content)   # B2：原子 seq 取号

    def _leg_update(task_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE task_id=? AND level='L3'",
                     (*fields.values(), task_id))
        conn.commit()

    def prepare(state: L3State) -> dict:
        tid = state["task_id"]
        runner = deps.runner
        # F 卡 C5（2026-11-12）：primary_language 四语言分发镜像与服务启动
        # （go=go126/Go 服务；java=jdk17-maven+spring-boot:run；php=php83+内置服务器；
        #   js=node20-jest+node 服务；python=py312+uvicorn 旧路径逐字节不变）。
        # 冒烟执行器全语言统一=Python requests 打 127.0.0.1（同 C4 契约架构）——
        # java/php 镜像已烘焙 python3，js 镜像补 python3（Dockerfile 同 commit）；
        # go126 已烘焙 requests。冒烟脚本=build_smoke_script（语言无关）。
        lang = state.get("primary_language") or "python"
        image = {"go": config.IMAGE_GO, "java": config.IMAGE_JAVA,
                 "php": config.IMAGE_PHP, "javascript": config.IMAGE_JS,
                 "typescript": config.IMAGE_JS}.get(lang, config.IMAGE_PY)
        container = runner.ensure_container(image, state["container_name"], fresh=True)
        runner.copy_dir_into(container, state["workspace"])
        # 依赖三清单走 L1 同款 install_deps（uvicorn 等服务依赖随项目走），requests 为冒烟执行器
        from ..skills.builtin import probe_deps_manifest
        registry.invoke(tid, deps.skills["install_deps"], {
            "container_name": state["container_name"],
            "deps_manifest": probe_deps_manifest(Path(state["workspace"]))})
        port = int(config.L2_SERVICE_PORT)
        ws = Path(state["workspace"])
        if lang != "go":
            # go126 镜像已烘焙 requests+pytest；其余语言容器运行时补——pytest 为
            # L4 验收回归模块（D6 转化产物）的执行环境（2026-11-12 C5 补环）
            runner.exec_in(container, ["pip", "install", "-q", "requests", "pytest"],
                           timeout_s=300)
        if lang == "go":
            # F 卡 #8：go126 镜像烘焙 python3+requests（#7 Dockerfile），Go 服务走 go build
            if not _detect_go_entry(ws):
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 Go 服务入口", finished_at=_now())
                msg(state["session_id"], "[L3·prepare] 未找到 Go 服务入口：本级如实终止")
                return {"done": True, "verdict": "stop", "stop_reason": "未找到 Go 服务入口"}
            ok, why, health, actual_port = _start_service_go(runner, container, ws, port)
            port = actual_port                    # 服务硬编码端口时跟随实际（日志解析兜底）
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"Go 服务启动失败：{why}", finished_at=_now())
                msg(state["session_id"], f"[L3·prepare] Go 服务启动失败：{why}")
                return {"done": True, "verdict": "stop",
                        "stop_reason": f"Go 服务启动失败：{why}"}
            msg(state["session_id"],
                f"[L3·prepare] Go 服务就绪（127.0.0.1:{port}，健康 {health or '(tcp)'}）")
            state["service_port"] = port
        elif lang == "java":
            main_cls = _detect_java_main(ws)
            if not main_cls:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 Java 服务入口（src/main/java 无 @SpringBootApplication 或 main 类）",
                            finished_at=_now())
                msg(state["session_id"], "[L3·prepare] 未找到 Java 服务入口：本级如实终止")
                return {"done": True, "verdict": "stop", "stop_reason": "未找到 Java 服务入口"}
            ok, why = _start_service_java(runner, container, main_cls, port)
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"Java 服务启动失败：{why}", finished_at=_now())
                msg(state["session_id"], f"[L3·prepare] Java 服务启动失败：{why}")
                return {"done": True, "verdict": "stop",
                        "stop_reason": f"Java 服务启动失败：{why}"}
            msg(state["session_id"],
                f"[L3·prepare] Java 服务就绪（{main_cls}，127.0.0.1:{port}）")
            state["service_port"] = port
        elif lang == "php":
            ok, why = _start_service_php(runner, container, port)
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"{why}（L3）", finished_at=_now())
                msg(state["session_id"], f"[L3·prepare] {why}：本级如实终止")
                return {"done": True, "verdict": "stop", "stop_reason": f"{why}（L3）"}
            msg(state["session_id"],
                f"[L3·prepare] PHP 服务就绪（内置服务器 127.0.0.1:{port}）")
            state["service_port"] = port
        elif lang in ("javascript", "typescript"):
            entry = _detect_js_entry(ws)
            if not entry:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 JS 服务入口（package.json main 或含 listen( 的 server/app/index/main.js）",
                            finished_at=_now())
                msg(state["session_id"], "[L3·prepare] 未找到 JS 服务入口：本级如实终止")
                return {"done": True, "verdict": "stop", "stop_reason": "未找到 JS 服务入口"}
            ok, why = _start_service_js(runner, container, entry, port)
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"JS 服务启动失败：{why}"[:300], finished_at=_now())
                msg(state["session_id"], f"[L3·prepare] JS 服务启动失败：{why}"[:200])
                return {"done": True, "verdict": "stop",
                        "stop_reason": f"JS 服务启动失败：{why}"[:300]}
            msg(state["session_id"],
                f"[L3·prepare] JS 服务就绪（node {entry}，127.0.0.1:{port}）")
            state["service_port"] = port
        else:
            app_module = _detect_app_module(ws)
            if not app_module:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 FastAPI 应用模块", finished_at=_now())
                msg(state["session_id"], "[L3·prepare] 未找到 FastAPI 应用模块：本级如实终止")
                return {"done": True, "verdict": "stop", "stop_reason": "未找到 FastAPI 应用模块"}
            ok, why = _start_service(runner, container, app_module, port)
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"服务启动失败：{why}", finished_at=_now())
                msg(state["session_id"], f"[L3·prepare] 服务启动失败：{why}")
                return {"done": True, "verdict": "stop", "stop_reason": f"服务启动失败：{why}"}
            state["service_port"] = port
        rows = conn.execute(
            """SELECT method, path, operation_id, params, request_schema FROM interface_endpoints
               WHERE task_id=? ORDER BY id""", (tid,)).fetchall()
        endpoints = [{"method": r["method"], "path": r["path"],
                      "operation_id": r["operation_id"],
                      "params": json.loads(r["params"] or "[]"),
                      "request_schema": json.loads(r["request_schema"])
                      if r["request_schema"] else None} for r in rows]
        if not endpoints:
            _leg_update(tid, strategy="degraded", status="skipped", verdict="skipped",
                        stop_reason="无端点清单（L2 未产出），场景生成无静态闸基准",
                        finished_at=_now())
            msg(state["session_id"], "[L3·prepare] 无端点清单：诚实降级，本级 skipped")
            return {"done": True, "verdict": "skipped", "stop_reason": "无端点清单"}
        msg(state["session_id"], f"[L3·prepare] 服务就绪（127.0.0.1:{port}），"
                                 f"端点清单 {len(endpoints)} 条（静态闸基准）")
        return {"service_port": port, "endpoints": endpoints}

    def gen(state: L3State) -> dict:
        tid = state["task_id"]
        from ..skills.llm_profile import effective_key, missing_key_message
        api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析
        if not api_key:
            reason = missing_key_message({"provider": "未配置", "key_env": "见设置页"})
            _leg_update(tid, status="skipped", verdict="skipped",
                        stop_reason=reason + "：场景生成跳过（诚实降级）",
                        finished_at=_now())
            msg(state["session_id"], f"[L3·gen] {reason}：场景生成跳过（诚实降级）")
            return {"done": True, "verdict": "skipped", "stop_reason": "无 LLM 密钥"}
        from ..skills.llm_client import make_chat
        llm = make_chat(phase="l3_gen_scenarios", api_key=api_key, temperature=0.2,
                        timeout=90, max_retries=2)
        eps = state["endpoints"]
        ep_text = "\n".join(f"- {e['method']} {e['path']}" for e in eps)
        from ..skills.scenario_gen import SCENARIO_SYSTEM, build_scenario_prompt
        resp = llm.invoke([("system", SCENARIO_SYSTEM),
                           ("user", build_scenario_prompt(state.get("requirement", ""), eps))])
        log_llm_call(tid, "l3_gen_scenarios", resp)
        valid, rejected = parse_scenarios(resp.content, eps)
        n = save_scenarios(conn, tid, valid)
        summary = (f"[L3·gen] 场景 {n} 个落库（pending 待冒烟）"
                   + (f"；剔除 {len(rejected)} 条：{'；'.join(rejected[:3])}" if rejected else ""))
        msg(state["session_id"], summary)
        if n == 0:
            _leg_update(tid, status="skipped", verdict="skipped",
                        stop_reason="场景生成 0 条（生成或静态闸全否）", finished_at=_now())
            return {"done": True, "verdict": "skipped", "scenarios": [],
                    "stop_reason": "场景生成 0 条"}
        return {"scenarios": valid}

    def smoke(state: L3State) -> dict:
        tid = state["task_id"]
        runner = deps.runner
        ws = Path(state["workspace"])
        base = f"http://127.0.0.1:{state.get('service_port', config.L2_SERVICE_PORT)}"
        (ws / "smoke_l3.py").write_text(
            build_smoke_script(state["scenarios"], base), encoding="utf-8")
        container = runner.client.containers.get(state["container_name"])
        runner.copy_dir_into(container, ws)
        # python3 统一（2026-11-15 实锤：go126 只有 python3，`python` not found→
        # 冒烟脚本对 Go 容器从来跑不起来）；py312/java/php 四镜像 python3 全在
        r = runner.exec_in(container, ["python3", "smoke_l3.py"], timeout_s=300)
        raw = r.stdout or ""
        if "SMOKE_JSON=" not in raw:
            _leg_update(tid, status="failed", verdict="stop",
                        stop_reason=f"冒烟脚本执行失败：{(r.stderr or '')[:200]}",
                        finished_at=_now())
            msg(state["session_id"], "[L3·smoke] 冒烟脚本执行失败（详见 leg stop_reason）")
            return {"done": True, "verdict": "stop",
                    "stop_reason": "冒烟脚本执行失败"}
        results = json.loads(raw.split("SMOKE_JSON=", 1)[1].strip().splitlines()[0])
        counts = apply_smoke_results(conn, tid, results)
        summary = {"auto_pass": counts["auto_pass"],
                   "pending_confirm": counts["pending_confirm"],
                   "total": len(state["scenarios"])}
        conn.execute("DELETE FROM evaluation_rounds WHERE task_id=? AND level='L3'", (tid,))
        conn.execute(
            """INSERT INTO evaluation_rounds(task_id, level, round_no, passed, failed,
                                             skipped, verdict, failure_kind, stop_reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, "L3", 1, counts["auto_pass"], counts["pending_confirm"], 0,
             "semi_auto", "product_defect" if counts["pending_confirm"] else "none",
             "冒烟失败场景转人工确认（D5）" if counts["pending_confirm"] else "冒烟全过"))
        conn.commit()
        _leg_update(tid, verdict="semi_auto",
                    summary=json.dumps(summary, ensure_ascii=False))
        msg(state["session_id"],
            f"[L3·smoke] 场景 {summary['total']} 个：自动通过 {counts['auto_pass']}，"
            f"待人工确认 {counts['pending_confirm']}（失败场景经 API 回库定性，D5）")
        return {"verdict": "semi_auto", **summary}

    def report(state: L3State) -> dict:
        tid = state["task_id"]
        verdict = state.get("verdict", "semi_auto")
        _leg_update(tid, status="done", finished_at=_now())
        conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?",
                     (state["session_id"],))
        conn.commit()
        msg(state["session_id"], f"[L3·report] 本级终态 {verdict}｜"
                                 f"{state.get('stop_reason', '半自动：失败场景待人工确认')}")
        return {"done": True}

    return {"prepare": prepare, "gen": gen, "smoke": smoke, "report": report}


def build_l3_graph(deps):
    nodes = make_l3_nodes(deps)
    g = StateGraph(L3State)
    for name in ("prepare", "gen", "smoke", "report"):
        g.add_node(name, nodes[name])
    g.set_entry_point("prepare")
    g.add_conditional_edges("prepare",
                            lambda s: "report" if s.get("done") else "gen",
                            {"report": "report", "gen": "gen"})
    g.add_conditional_edges("gen",
                            lambda s: "report" if s.get("done") else "smoke",
                            {"report": "report", "smoke": "smoke"})
    g.add_edge("smoke", "report")
    g.add_edge("report", END)
    return g.compile()


def _leg_upsert_l3(conn, task_id: int, trace_from: int | None = None, **fields) -> int:
    row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level='L3'",
                       (task_id,)).fetchone()
    base = {"strategy": "semi_auto", "status": "queued", "trace_from": trace_from}
    base.update({k: v for k, v in fields.items()})
    if row:
        sets = ", ".join(f"{k}=?" for k in base)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE id=?", (*base.values(), row["id"]))
        return row["id"]
    cols = ", ".join(base)
    cur = conn.execute(
        f"INSERT INTO test_levels(task_id, level, {cols}) VALUES (?, 'L3', {', '.join('?' for _ in base)})",
        (task_id, *base.values()))
    conn.commit()
    return cur.lastrowid


def run_l3_task(task_id: int, trace_from: int | None = None) -> dict:
    """L3 leg 入口：容器无论成败一律回收。pending_confirm 场景留在库中待人确认。"""
    from .nodes import GraphDeps, make_deps
    deps: GraphDeps = make_deps()
    conn = deps.conn
    task = conn.execute(
        """SELECT t.requirement_text, p.primary_language FROM test_tasks t
           JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,)).fetchone()
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        _leg_upsert_l3(conn, task_id, trace_from, status="failed", verdict="stop",
                       stop_reason="无工作区（L1 未跑或已被清理）")
        return {"verdict": "stop", "stop_reason": "无工作区"}
    _leg_upsert_l3(conn, task_id, trace_from, status="running")
    cur = conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
                       (task_id,))
    conn.commit()
    init: L3State = {
        "task_id": task_id, "session_id": cur.lastrowid,
        "primary_language": task["primary_language"] or "",
        "workspace": str(ws), "container_name": f"mta-run-{task_id}",
        "requirement": task["requirement_text"] or "",
    }
    graph = build_l3_graph(deps)
    try:
        return graph.invoke(init, {"recursion_limit": 30})
    except Exception as e:
        sets = ", ".join(f"{k}=?" for k in ("status", "stop_reason", "finished_at"))
        conn.execute(f"UPDATE test_levels SET {sets} WHERE task_id=? AND level='L3'",
                     ("failed", f"{type(e).__name__}: {e}"[:300], _now(), task_id))
        conn.commit()
        raise
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{task_id}")
        except Exception as e:  # noqa: BLE001
            log.warning("L3 容器回收失败: %s", e)
