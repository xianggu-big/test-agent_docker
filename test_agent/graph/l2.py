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
from ..skills.app_logs import APP_LOG_CONTAINER_PATH, collect_and_store_app_logs
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
    primary_language: str         # F 卡 #7（2026-11-10）：go → go126 镜像+Go 服务启动
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
    last_stderr: str             # run 节点回传；不声明则被 LangGraph 过滤，evaluate 的
                                 # raw 尾巴 stderr 恒空=归因断点（407 证据蒸发家族）
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
            # proto 兜底档产物是 service/rpc 异形（无 method/path）——HTTP 契约轨
            # 只收同形端点，异形如实滤除留痕（不落 interface_endpoints，2026-11-15）；
            # None 语义保留：无可用提取路径时 endpoints 仍为 None（诚实降级契约）
            http_eps = None if eps is None else [
                e for e in eps if e.get("method") and e.get("path")]
            proto_only = eps is not None and len(http_eps or []) != len(eps)
            conn.execute("DELETE FROM interface_endpoints WHERE task_id=?", (task_id,))
            for e in http_eps or []:
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
            if proto_only:
                return json.dumps({"endpoints": http_eps, "how": how,
                                   "note": "proto 兜底档产物为 gRPC 异形端点，"
                                           "HTTP 契约轨无可渲染端点（grpc 轨另走 parse_proto）"},
                                  ensure_ascii=False)
            return json.dumps({"endpoints": http_eps, "how": how}, ensure_ascii=False)
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
            from ..skills.llm_profile import effective_key
            api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析
            requirement = args.get("requirement") or ""
            if api_key and requirement and config.L2_BLACKBOX_ENABLED:
                llm = make_chat(phase="L2.gen_requirement", api_key=api_key, temperature=0.1,
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


# —— F 卡阶段 1 #7（L2 契约轨接 Go，2026-11-10）：Go 服务启动与健康检查 ——

_GO_ENTRY_FILES = ("main.go", "server.go", "cmd.go")


def _detect_go_entry(ws: Path) -> str | None:
    """确定性地找 Go 服务入口：根目录 main.go/server.go（含 func main 优先）。
    gin-master 形态=根目录 main.go。找不到返回 None（诚实降级）。"""
    ws = Path(ws)
    candidates = [ws / n for n in _GO_ENTRY_FILES if (ws / n).is_file()]
    candidates += [p for p in sorted(ws.glob("*.go")) if p.name not in _GO_ENTRY_FILES]
    for p in candidates:
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "func main(" in t:
            return p.name
    return None


_GO_HEALTH_PATHS = ("/ping", "/healthz", "/health")   # gin 社区惯例健康路径


def _start_service_go(runner, container, ws: Path,
                      port: int) -> tuple[bool, str, str, int]:
    """Go 服务启动（F 卡 #7）：go build -o /tmp/app && 后台运行 → 探测就绪。

    返回 (就绪?, 失败说明, 健康路径, 实际端口)。健康路径从 _GO_HEALTH_PATHS 逐个试，
    全不通时 TCP 通也算就绪（契约用例对 404 一样有断言价值——路由清单已落库）。
    实际端口兜底（2026-11-15 Go L2 实锤）：服务忽略 --port 硬编码监听时，探针在
    下发端口上空等——从应用日志解析 gin 实际监听端口复探，契约 BASE 跟随实际端口。"""
    if not (ws / "go.mod").is_file():
        return False, "工作区无 go.mod（非 Go module 项目）", "", port
    runner.exec_in(container, ["sh", "-c", "go build -o /tmp/goapp ."],
                   timeout_s=300, workdir="/work")
    runner.exec_in(container, ["sh", "-c",
                   f"mkdir -p /work/logs && cd /work && "
                   f"nohup /tmp/goapp --port {port} > {APP_LOG_CONTAINER_PATH} 2>&1 &"],
                   timeout_s=15)
    wait = int(config.L2_SERVICE_WAIT_S)
    chk = (
        "import time,socket,sys,urllib.request\n"
        f"deadline=time.time()+{wait}\n"
        "paths=['" + "','".join(_GO_HEALTH_PATHS) + "']\n"
        "ready=False; path=''\n"
        "while time.time()<deadline:\n"
        "    for p in paths:\n"
        "        try:\n"
        f"            urllib.request.urlopen('http://127.0.0.1:{port}'+p, timeout=2)\n"
        "            ready=True; path=p; break\n"
        "        except Exception:\n"
        "            pass\n"
        "    if not ready:\n"
        "        try:\n"
        f"            s=socket.create_connection(('127.0.0.1',{port}), timeout=2)\n"
        "            s.close(); ready=True; path='(tcp)'\n"
        "        except Exception:\n"
        "            pass\n"
        "    if ready:\n"
        "        print('READY', path); sys.exit(0)\n"
        "    time.sleep(1)\n"
        "print('NOT_READY'); sys.exit(1)\n")
    r = runner.exec_in(container, ["python3", "-c", chk], timeout_s=wait + 15)
    if r.exit_code == 0 and "READY" in r.stdout:
        parts = (r.stdout or "").split()
        return True, "", (parts[1] if len(parts) > 1 else ""), port
    # 实际端口兜底：应用日志找 gin 实际监听端口，复探成功则以实际端口为准
    tail = runner.exec_in(container, ["sh", "-c", f"tail -n 40 {APP_LOG_CONTAINER_PATH}"],
                          timeout_s=10)
    log_text = (tail.stdout or "") + (tail.stderr or "")
    m = re.search(r"Listening and serving HTTP on 127\.0\.0\.1:(\d+)", log_text) or \
        re.search(r"127\.0\.0\.1:(\d+)", log_text)
    if m and int(m.group(1)) != port:
        actual = int(m.group(1))
        chk2 = (
            "import time,socket,sys,urllib.request\n"
            "paths=['" + "','".join(_GO_HEALTH_PATHS) + "']\n"
            f"deadline=time.time()+10\n"
            "while time.time()<deadline:\n"
            "    for p in paths:\n"
            "        try:\n"
            f"            urllib.request.urlopen('http://127.0.0.1:{actual}'+p, timeout=2)\n"
            "            print('READY', p); sys.exit(0)\n"
            "        except Exception:\n"
            "            pass\n"
            "    try:\n"
            f"        s=socket.create_connection(('127.0.0.1',{actual}), timeout=2)\n"
            "        s.close(); print('READY (tcp)'); sys.exit(0)\n"
            "    except Exception:\n"
            "        pass\n"
            "    time.sleep(1)\n"
            "print('NOT_READY'); sys.exit(1)\n")
        r2 = runner.exec_in(container, ["python3", "-c", chk2], timeout_s=25)
        if r2.exit_code == 0 and "READY" in (r2.stdout or ""):
            parts2 = (r2.stdout or "").split()
            health = f"{parts2[1] if len(parts2) > 1 else '(tcp)'}@日志解析端口"
            log.warning("Go 服务忽略 --port（实际监听 %d，下发 %d）——契约 BASE 跟随实际端口",
                        actual, port)
            return True, "", health, actual
    return False, (log_text or "Go 服务未就绪")[-300:], "", port


def _start_service(runner, container, app_module: str, port: int) -> tuple[bool, str]:
    # P2-6（2026-10-31 体检）：app_module 拼进 sh -c，文件名含 shell 元字符=命令注入；
    # 模块名必须是合法 Python 标识符（点号分层允许），否则拒绝起服务（fail-closed）
    import re as _re
    if not _re.fullmatch(r"[A-Za-z_][\w.]*(\.[A-Za-z_]\w*)*", app_module or ""):
        return False, f"app_module 非法（非标识符）：{app_module[:60]}"
    """容器内后台起 uvicorn 并轮询健康（/openapi.json）；返回 (就绪?, 失败说明）。

    黑盒能力补齐阶段1·日志分流：服务进程输出不再丢 /tmp/l2svc.log，改重定向
    /work/logs/app.log（测试进程输出走原 pytest 通道——两路分流），按轮快照出宿主。"""
    runner.exec_in(container, ["sh", "-c",
                   f"mkdir -p /work/logs && cd /work && nohup python -m uvicorn {app_module}:app "
                   f"--host 127.0.0.1 --port {port} > {APP_LOG_CONTAINER_PATH} 2>&1 &"],
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
    tail = runner.exec_in(container, ["sh", "-c", f"tail -n 20 {APP_LOG_CONTAINER_PATH}"],
                          timeout_s=10)
    return False, (tail.stdout or tail.stderr or "服务未就绪")[-300:]


_JAVA_MAIN_RE = re.compile(r"@(SpringBootApplication|RestController)?")


def _detect_js_entry(ws: Path) -> str | None:
    """JS 服务入口探测（F 卡 C5，2026-11-12）：package.json main 字段 >
    server.js/app.js/index.js/main.js（根目录，含 listen( 或 createServer 特征）。
    返回容器内相对文件名。"""
    pkg = ws / "package.json"
    if pkg.is_file():
        try:
            main = json.loads(pkg.read_text(encoding="utf-8", errors="ignore")).get("main")
            # P2-6 同款（2026-11-15 闲时找bug）：entry 会拼进 sh -c——main 含 shell
            # 元字符=命令注入（Python 版 _start_service 已有同校验，JS 版漏）。
            # 白名单=安全相对路径字符集 + 禁 .. 穿越
            import re as _re
            if (isinstance(main, str) and (ws / main).is_file()
                    and _re.fullmatch(r"[A-Za-z0-9_][\w\-/]*\.js", main)
                    and ".." not in main):
                return main
        except (json.JSONDecodeError, OSError):
            pass
    for name in ("server.js", "app.js", "index.js", "main.js"):
        p = ws / name
        if not p.is_file():
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "listen(" in t or "createServer" in t:
            return name
    return None


def _start_service_js(runner, container, entry: str, port: int) -> tuple[bool, str]:
    """JS 服务后台启动（node <entry>）→ TCP 就绪轮询（与 _start_service_go 同构）。

    P2-6 同款 fail-closed（2026-11-15 闲时找bug）：entry 拼进 sh -c——含 shell
    元字符/穿越即拒绝起服务（_detect_js_entry 已白名单，此处双保险防新调用点绕过）。"""
    import re as _re
    if (not _re.fullmatch(r"[A-Za-z0-9_][\w\-/]*\.js", entry or "")
            or ".." in (entry or "")):
        return False, f"JS 入口非法（含元字符/穿越）：{(entry or '')[:60]}"
    runner.exec_in(container, ["sh", "-c",
                   f"mkdir -p /work/logs && cd /work && "
                   f"nohup node {entry} > {APP_LOG_CONTAINER_PATH} 2>&1 &"],
                   timeout_s=15)
    wait = int(config.L2_SERVICE_WAIT_S)
    chk = "\n".join([
        "import time,socket,sys",
        f"deadline=time.time()+{wait}",
        "while True:",
        "    s=socket.socket()",
        "    s.settimeout(2)",
        "    try:",
        f"        s.connect(('127.0.0.1',{port}))",
        "        print('READY'); sys.exit(0)",
        "    except OSError:",
        "        if time.time()>deadline: print('NOT_READY'); sys.exit(1)",
        "        time.sleep(1)",
        "    finally:",
        "        s.close()",
    ])
    r = runner.exec_in(container, ["python3", "-c", chk], timeout_s=wait + 15)
    if r.exit_code != 0 or "READY" not in (r.stdout or ""):
        tail = runner.exec_in(container, ["sh", "-c", f"tail -n 20 {APP_LOG_CONTAINER_PATH}"],
                              timeout_s=15)
        return False, f"JS 服务未就绪（127.0.0.1:{port}），应用日志尾部：{(tail.stdout or '')[-300:]}"
    return True, ""


def _php_ready_check(port: int) -> str:
    """PHP 内置服务器就绪轮询脚本（python3 -c 执行，java/php 镜像已烘焙 python3）。

    HTTPError（4xx/5xx）也算就绪——收到 HTTP 应答本身即服务在跑的证据；
    旧脚本把 404 当未就绪，根路径非 200 的 PHP 项目（api 型无 / 页）永远起不来
    （2026-11-13 C5 真机实锤）。"""
    return "\n".join([
        "import time,sys,urllib.request,urllib.error",
        f"deadline=time.time()+{int(config.L2_SERVICE_WAIT_S)}",
        "while True:",
        "    try:",
        f"        urllib.request.urlopen('http://127.0.0.1:{port}', timeout=2)",
        "        print('READY'); sys.exit(0)",
        "    except urllib.error.HTTPError:",
        "        print('READY'); sys.exit(0)   # 4xx/5xx 也是服务在应答",
        "    except Exception:",
        "        if time.time()>deadline: print('NOT_READY'); sys.exit(1)",
        "        time.sleep(1)",
    ])


def _start_service_php(runner, container, port: int) -> tuple[bool, str]:
    """PHP 内置服务器后台启动（php -S，index.php 入口惯例）→ HTTP 就绪轮询。"""
    runner.exec_in(container, ["sh", "-c",
                   f"mkdir -p /work/logs && cd /work && "
                   f"nohup php -S 127.0.0.1:{port} index.php > {APP_LOG_CONTAINER_PATH} 2>&1 &"],
                   timeout_s=15)
    r = runner.exec_in(container, ["python3", "-c", _php_ready_check(port)],
                       timeout_s=int(config.L2_SERVICE_WAIT_S) + 15)
    if r.exit_code != 0 or "READY" not in (r.stdout or ""):
        return False, "PHP 内置服务器未就绪"
    return True, ""


def _detect_java_main(ws: Path) -> str | None:
    """Java 服务入口探测（F 卡能力补齐，2026-11-12）：src/main/java 下
    @SpringBootApplication 类 > 含 main 方法的 Application/Main 类。返回全限定类名。"""
    src_root = ws / "src" / "main" / "java"
    if not src_root.is_dir():
        return None
    spring, with_main = [], []
    for p in src_root.rglob("*.java"):
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(src_root).with_suffix("")
        fqcn = ".".join(rel.parts)
        if "@SpringBootApplication" in t:
            spring.append(fqcn)
        elif re.search(r"public\s+static\s+void\s+main\s*\(", t):
            with_main.append(fqcn)
    return (spring or with_main or [None])[0]


def _start_service_java(runner, container, main_class: str, port: int) -> tuple[bool, str]:
    """Java 服务后台启动（spring-boot:run 优先，退化 exec:java 主类）→ TCP 就绪探测。

    就绪窗口 min 180s：spring-boot:run 的 maven 启动开销（JVM+依赖解析+插件 fork）
    实测 ~99s（应用自身仅 1.7s）——L2_SERVICE_WAIT_S=30 对 Java 结构性不够，
    python uvicorn/php -S/node 无此层开销不受影响（2026-11-13 真机实锤）。"""
    import re as _re
    if not _re.fullmatch(r"[\w.]+", main_class or ""):
        return False, f"主类名非法：{main_class[:60]}"
    runner.exec_in(container, ["sh", "-c",
                   f"mkdir -p /work/logs && cd /work && "
                   f"nohup mvn -q spring-boot:run "
                   f"-Dspring-boot.run.arguments=--server.port={port} "
                   f"> {APP_LOG_CONTAINER_PATH} 2>&1 &"],
                   timeout_s=15)
    wait = max(int(config.L2_SERVICE_WAIT_S), 180)
    chk_script = "\n".join([
        "import time,socket,sys",
        f"deadline=time.time()+{wait}",
        "while True:",
        "    try:",
        f"        s=socket.create_connection(('127.0.0.1',{port}), timeout=2)",
        "        s.close(); print('READY'); sys.exit(0)",
        "    except Exception:",
        "        if time.time()>deadline: print('NOT_READY'); sys.exit(1)",
        "        time.sleep(1)",
    ])
    r = runner.exec_in(container, ["python3", "-c", chk_script], timeout_s=wait + 15)
    if r.exit_code == 0 and "READY" in (r.stdout or ""):
        return True, ""
    tail = runner.exec_in(container, ["sh", "-c", f"tail -n 20 {APP_LOG_CONTAINER_PATH}"],
                          timeout_s=10)
    return False, (tail.stdout or tail.stderr or "Java 服务未就绪")[-300:]


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
        conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES(?,?,?,?)",
                     (session_id, seq, "assistant", content))
        conn.commit()

    def _collect_app_logs(state: L2State, round_no: int) -> None:
        # 黑盒能力补齐阶段1：应用日志分流落库（采集→宿主快照→解析→app_logs）。
        # 失败只留痕不炸主链路——日志账本缺席不该拖垮测试执行本身。
        try:
            info = collect_and_store_app_logs(
                deps.runner, deps.runner.client.containers.get(state["container_name"]),
                conn, state["task_id"], round_no, Path(state["workspace"]), lang="python")
            if info.get("stored"):
                msg(state["session_id"],
                    f"[L2·app-logs] 第{round_no}轮应用日志：错误 {info['error_count']} 行 / "
                    f"警告 {info['warn_count']} 行入库（source={info['source']}，"
                    f"首错行 {info['first_error_line']}，快照 {info['host_path']}）")
            elif info.get("source") != "empty":
                msg(state["session_id"],
                    f"[L2·app-logs] 第{round_no}轮应用日志无错误/警告行"
                    f"（{info['log_lines']} 行已快照 {info['host_path']}）")
        except Exception as e:      # noqa: BLE001 —— 观测层缺席不炸执行主链路
            log.warning("应用日志分流失败 task=%s: %s", state["task_id"], e)

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
        # F 卡阶段 1 #7 + 能力补齐（2026-11-10/12）：契约用例=Python requests 黑盒
        # （打 127.0.0.1 的服务）——四语言按 primary_language 分发镜像与服务启动：
        # go=go126（烘焙 python3）/java=jdk17-maven+python3（pom 同容器 mvn run）/
        # php=php83+python3（内置服务器）/python=py312+uvicorn（旧路径逐字节不变）
        lang = state.get("primary_language") or "python"
        is_go = lang == "go"   # fc6406d 重构把赋值删了但判断残留——UnboundLocalError 运行时炸弹（2026-11-13 修）
        image = {"go": config.IMAGE_GO, "java": config.IMAGE_JAVA,
                 "php": config.IMAGE_PHP}.get(lang, config.IMAGE_PY)
        container = runner.ensure_container(image, state["container_name"], fresh=True)
        runner.copy_dir_into(container, state["workspace"])
        deps.registry.invoke(tid, deps.skills["install_deps"], {
            "container_name": state["container_name"],
            "has_requirements": (Path(state["workspace"]) / "requirements.txt").exists()})
        # 测试执行器依赖（v0 单容器：requests+jsonschema 随开关装，同 hypothesis 先例）
        if not is_go:
            runner.exec_in(container, ["pip", "install", "-q", "requests", "jsonschema"],
                           timeout_s=300)
        if is_go:
            from ..skills.llm_profile import effective_key
            ws = Path(state["workspace"])
            if not _detect_go_entry(ws):
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 Go 服务入口（根目录 main.go/server.go 含 func main）",
                            finished_at=_now())
                msg(state["session_id"], "[L2·provision] 未找到 Go 服务入口：本级如实终止")
                return {"done": True, "verdict": "stop", "service_ready": False,
                        "stop_reason": "未找到 Go 服务入口"}
            port = int(config.L2_SERVICE_PORT)
            ok, why, health, actual_port = _start_service_go(runner, container, ws, port)
            port = actual_port                    # 服务硬编码端口时跟随实际（日志解析兜底）
            if not ok:
                _collect_app_logs(state, 1)
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"Go 服务启动失败：{why}",
                            finished_at=_now())
                msg(state["session_id"], f"[L2·provision] Go 服务启动失败：{why}")
                return {"done": True, "verdict": "stop", "service_ready": False,
                        "stop_reason": f"Go 服务启动失败：{why}"}
            # requests/jsonschema/pytest 已烘焙进 go126 镜像（F 卡 #7 Dockerfile）
            msg(state["session_id"],
                f"[L2·provision] Go 服务就绪（go build + 后台运行，127.0.0.1:{port}，"
                f"健康路径 {health or '(tcp)'}）；契约用例=Python requests 黑盒（同容器打 127.0.0.1）")
            return {"service_port": port, "service_ready": True}
        if lang == "java":
            # Java 服务启动（能力补齐，2026-11-12）：探测 main 类 → mvn spring-boot:run
            # 或 mvn compile exec:java（demo 无 spring 时 exec 主类）；健康 TCP 兜底
            from ..skills.llm_profile import effective_key
            main_cls = _detect_java_main(Path(state["workspace"]))
            if not main_cls:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason="未找到 Java 服务入口（src/main/java 无 @SpringBootApplication 或 main 类）",
                            finished_at=_now())
                msg(state["session_id"], "[L2·provision] 未找到 Java 服务入口：本级如实终止")
                return {"done": True, "verdict": "stop", "service_ready": False,
                        "stop_reason": "未找到 Java 服务入口"}
            port = int(config.L2_SERVICE_PORT)
            ok, why = _start_service_java(runner, container, main_cls, port)
            if not ok:
                _collect_app_logs(state, 1)
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=f"Java 服务启动失败：{why}", finished_at=_now())
                msg(state["session_id"], f"[L2·provision] Java 服务启动失败：{why}")
                return {"done": True, "verdict": "stop", "service_ready": False,
                        "stop_reason": f"Java 服务启动失败：{why}"}
            msg(state["session_id"],
                f"[L2·provision] Java 服务就绪（{main_cls}，127.0.0.1:{port}）；契约用例=Python requests 黑盒")
            return {"service_port": port, "service_ready": True}
        if lang == "php":
            # PHP 服务启动（能力补齐）：php -S 内置服务器（index.php 入口惯例，零探测）
            # （启动/就绪轮询已抽模块级 _start_service_php，L3 C5 复用，2026-11-12）
            port = int(config.L2_SERVICE_PORT)
            ok, why = _start_service_php(runner, container, port)
            if not ok:
                _leg_update(tid, status="failed", verdict="stop",
                            stop_reason=why, finished_at=_now())
                msg(state["session_id"], f"[L2·provision] {why}：本级如实终止")
                return {"done": True, "verdict": "stop", "service_ready": False,
                        "stop_reason": why}
            msg(state["session_id"],
                f"[L2·provision] PHP 服务就绪（内置服务器 127.0.0.1:{port}）")
            return {"service_port": port, "service_ready": True}
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
            # 服务起不来的日志正是环境类证据（黑包阶段1）：失败路径也分流落库（round 1）
            _collect_app_logs(state, 1)
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
        # 应用运行时日志分流（黑包阶段1）：本轮服务进程输出快照+解析+app_logs 落库
        _collect_app_logs(state, int(state.get("round_no", 1) or 1))
        return {"last_exit": r["exit"], "last_stdout": r["stdout"], "last_stderr": r["stderr"]}

    def evaluate(state: L2State) -> dict:
        tid = state["task_id"]
        stdout = state.get("last_stdout", "")
        # Loop-1：判读抽纯函数 judge_l2（P2-5a 修复；下方消息行沿用同名局部量）
        # 2026-11-15 修：`or 1` 把合法的 exit=0 强转成 1——L2 全过被误判
        # "无可执行用例"→stop（E9 手动波证据留痕实锤：raw 显示 exit=0/3 passed）。
        # None 缺省仍按 1（未知当失败，保守方向不变）
        _last_exit_raw = state.get("last_exit")
        _last_exit = 1 if _last_exit_raw is None else int(_last_exit_raw)
        summary, verdict, reason = judge_l2(stdout, _last_exit,
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
        # 证据留痕（407 家族教训：last_stdout 不落库=归因链断点）：exit!=0 且判不出失败时
        # 必须能看到原始输出尾部——"通过 N 却 exit 非 0"这类矛盾才可事后定性
        if _last_exit != 0:
            msg(state["session_id"],
                f"[L2·evaluate·raw] exit={state.get('last_exit')}，输出尾部：\n"
                f"{(stdout or state.get('last_stderr', '') or '(空)')[-800:]}")
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
    """UTC 秒级时间戳（与全库 SQLite datetime('now') 同口径）——test_levels 全用它，
    本地时区会让级别卡时间与 agent_sessions.ended_at 并排时超前时区偏移。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ", "seconds")


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
    task = conn.execute(
        """SELECT t.requirement_text, p.primary_language FROM test_tasks t
           JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,)).fetchone()
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
        "primary_language": task["primary_language"] or "",   # F 卡 #7：go→go126+Go 服务
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
