"""L2 端点提取（D4 降级链）——契约黑盒轨的输入，纯确定性、零 LLM 零网络。

降级链（蓝图 §3.2/§11-D4）：
  ① 仓库自带 OpenAPI/Swagger spec（JSON）→ 全量解析：路径/方法/参数/请求体 schema/2xx 响应 schema
  ② 框架路由 AST 提取（Flask/FastAPI 无 spec 场景）→ P2（本轮占位不入链）
  ③ 无路由特征 → 诚实降级：返回 None，由调用方如实上报（绝不硬造端点）
  ④ .proto 文件解析（黑包阶段4，2026-11-07）：service/rpc/message 清单——gRPC 真值清单
    （放在 spec 之后：REST 项目带 .proto 的场景 spec 优先，纯 gRPC 项目走本源）

$ref 处理：resolve_refs 确定性展开 components/schemas 引用（深度限 12 防循环），生成物
自包含——契约校验侧（jsonschema.validate）不再依赖 spec 上下文。
"""
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
_SPEC_NAMES = ("openapi.json", "swagger.json")
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "vendor", ".venv", "venv",
              "dist", "build"}          # 与 routes.py/_SKIP 同口径（本文件此前无此常量）
_MAX_REF_DEPTH = 12

# —— proto3 文本解析（黑包阶段4）：正则子集，覆盖 demo/常见 proto3 形态 ——
# 已知边界：嵌套 message（大括号嵌套）不支持——正则取到首个 } 为止，留档不报错（诚实降级）
_SERVICE_RE = re.compile(r"service\s+(\w+)\s*\{([^}]*)\}")
_RPC_RE = re.compile(
    r"rpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)")
_MESSAGE_RE = re.compile(r"message\s+(\w+)\s*\{([^}]*)\}")
_FIELD_RE = re.compile(r"^\s*(?:repeated\s+|optional\s+)?([\w.]+)\s+(\w+)\s*=\s*\d+", re.M)


def parse_proto(text: str) -> dict:
    """proto3 文本 → {services:[{name, methods:[{name,input,output}]}], messages:{name:{field:type}}}。

    纯正则子集：不解析 option/enum/nested message（诚实边界，demo/常见形态覆盖）。"""
    doc = {"services": [], "messages": {}}
    for m in _MESSAGE_RE.finditer(text or ""):
        fields = {}
        for fm in _FIELD_RE.finditer(m.group(2) or ""):
            fields[fm.group(2)] = fm.group(1)
        doc["messages"][m.group(1)] = fields
    for sm in _SERVICE_RE.finditer(text or ""):
        methods = []
        for rm in _RPC_RE.finditer(sm.group(2) or ""):
            methods.append({"name": rm.group(1), "input": rm.group(3),
                            "output": rm.group(5)})
        if methods:
            doc["services"].append({"name": sm.group(1), "methods": methods})
    return doc


def proto_endpoints(doc: dict) -> list[dict]:
    """proto 文档 → 端点清单（与 openapi 端点平行的形状，gRPC 闸门/LLM 事实注入共用）。"""
    eps = []
    for svc in doc.get("services") or []:
        for m in svc.get("methods") or []:
            eps.append({"proto": True, "service": svc["name"], "rpc": m["name"],
                        "input": m["input"], "output": m["output"],
                        "input_fields": dict((doc.get("messages") or {}).get(m["input"], {}))})
    return eps


def resolve_refs(schema, components: dict, depth: int = 0):
    """确定性展开 schema 内的 $ref（OAS3 #/components/schemas/Name 与 Swagger2
    #/definitions/Name）；超深或环 → 原样返回（不炸）。

    list 必须下钻（2026-11-15 闲时找bug 修正）：allOf/anyOf/oneOf/items 是 list，
    旧实现在函数首行被 not isinstance(dict) 拦下原样返回——组合式 schema 内的
    $ref 永不展开，"生成物自包含"承诺落空（jsonschema.validate 拿到裸 $ref）。"""
    if depth > _MAX_REF_DEPTH:
        return schema
    if isinstance(schema, list):
        return [resolve_refs(x, components, depth + 1) for x in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        target = (components or {}).get("schemas", {}).get(name)
        if isinstance(target, dict):
            return resolve_refs(target, components, depth + 1)
        return schema
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        # Swagger 2 引用（parse_openapi 已把 definitions 并入 components.schemas）
        name = ref.rsplit("/", 1)[-1]
        target = (components or {}).get("schemas", {}).get(name)
        if isinstance(target, dict):
            return resolve_refs(target, components, depth + 1)
        return schema
    return {k: resolve_refs(v, components, depth + 1) for k, v in schema.items()}


def parse_openapi(spec: dict) -> list[dict]:
    """OpenAPI 3.x/Swagger 2 端点清单。只收 5 个 HTTP 方法；2xx 响应 schema 作契约 oracle。
    paths 非字典 / 无 JSON 响应体（如 204）→ schema 置空但 success_code 如实取文档值。"""
    # Swagger 2 的 definitions 并入 schemas 视图（resolve_refs 双前缀兼容两代规范）
    components = {"schemas": {**(spec.get("definitions") or {}),
                              **((spec.get("components") or {}).get("schemas") or {})}}
    out: list[dict] = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            params = []
            for p in shared_params + [p for p in (op.get("parameters") or []) if isinstance(p, dict)]:
                # OpenAPI 规范：in=path 参数隐含 required=true（大量 spec 漏写该字段；
                # 漏写时按 False 处理会让渲染层"跳过路径参数端点"失效→生成字面
                # /users/{id} URL 必假失败。2026-11-15 闲时找bug 修正）
                params.append({"name": p.get("name"), "in": p.get("in"),
                               "required": p.get("in") == "path" or bool(p.get("required")),
                               "schema": resolve_refs(p.get("schema") or {}, components)})
            req_schema = None
            content = ((op.get("requestBody") or {}).get("content") or {})
            if isinstance(content.get("application/json"), dict):
                req_schema = resolve_refs(content["application/json"].get("schema"), components)
            resp_schema = None
            resp_code: int | None = None
            documented = [str(c) for c in (op.get("responses") or {})]
            for code, resp in (op.get("responses") or {}).items():
                if str(code).startswith("2") and isinstance(resp, dict):
                    if resp_code is None and str(code).isdigit():
                        resp_code = int(code)            # 首个 2xx 即成功码（含 204 无响应体）
                    c2 = resp.get("content") or {}
                    if isinstance(c2.get("application/json"), dict):
                        resp_schema = resolve_refs(c2["application/json"].get("schema"), components)
                        break
            out.append({"method": method.upper(), "path": path,
                        "operation_id": op.get("operationId"),
                        "params": params, "request_schema": req_schema,
                        "response_schema": resp_schema, "source": "spec",
                        "success_code": resp_code or 200,
                        "documented_codes": documented})
    out.sort(key=lambda e: (e["path"], e["method"]))
    return out


def probe_endpoint_source(ws: Path) -> tuple[list[dict] | None, str]:
    """D4 降级链探测：返回 (端点清单|None, 来源说明)。None = 无可用提取路径（诚实降级）。"""
    ws = Path(ws)
    for name in _SPEC_NAMES:
        p = ws / name
        if not p.exists():
            continue
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("spec %s 解析失败，降级试下一个来源: %s", name, e)
            continue
        eps = parse_openapi(spec)
        if eps:
            return eps, f"spec:{name}"
        log.warning("spec %s 无可收端点，降级试下一个来源", name)
    # ⑤ Go 提取两级：swag 注释（schema 骨架更全）优先，0 端点降级 go_route——
    # 任务卡-F D4 第①级升级（2026-11-12）。
    # 顺序修正（2026-11-15 闲时找bug）：go 档提到 proto 之前——proto 产物是
    # service/rpc 异形（无 method/path），本链唯一消费方 l2 extract 落库即
    # KeyError 被吞成 failed；Go+proto 并存项目被 proto 截胡则 go 档永远轮不到，
    # L2 假报 no_spec。proto 档保留在最后兜底（grpc 轨的 parse_proto 独立直用不受影响）
    if any(p.suffix == ".go" for p in ws.rglob("*.go")
           if not any(part in _SKIP_DIRS for part in p.parts)):
        go_text = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(ws.rglob("*.go"))
            if not p.name.endswith("_test.go")
            and not any(part in _SKIP_DIRS for part in p.parts))
        eps = parse_swag_annotations(go_text)
        if eps:
            return eps, "go_swag"
        eps = parse_go_routes(go_text)
        if eps:
            return eps, "go_route"
        log.warning("go 路由提取 0 端点（诚实降级）")
    # ④ .proto 源（黑包阶段4）：HTTP 源全部落空后兜底——产物为 service/rpc 异形
    #（grpc 轨消费；L2 HTTP 轨拿到也只作"项目确有接口面"的留痕，不入 interface_endpoints）
    for p in sorted(ws.glob("*.proto")):
        try:
            doc = parse_proto(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            log.warning("proto %s 读取失败，降级试下一个来源: %s", p.name, e)
            continue
        eps = proto_endpoints(doc)
        if eps:
            return eps, f"proto:{p.name}"
        log.warning("proto %s 无可收 rpc，降级试下一个来源", p.name)
    return None, "no_spec"


def parse_go_routes(text: str) -> list[dict]:
    """Go 路由注册提取（任务卡-F2 阶段 1 #6，确定性正则零 LLM）：gin/echo/chi/
    net-http 主形态 `(r|e|router|mux|app).METHOD("path", ...)` 与
    `http.HandleFunc("path", ...)`。路径参数 `:id` → `{id}`（与 spec 端点口径一致）；
    schema 如实 None（静态提取无契约体）；success_code 恒 200（文档未声明）。
    注释行与非 HTTP 方法调用自然不匹配=剔除。"""
    route_re = re.compile(
        r"\b(?:[A-Za-z_]\w*)\.(GET|POST|PUT|DELETE|PATCH|get|post|put|delete|patch"
        r"|Get|Post|Put|Delete|Patch)"
        r'\s*\(\s*"(/[^"]*)"')
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ln in (text or "").splitlines():
        stripped = ln.strip()
        if stripped.startswith("//"):
            continue                                   # 注释行剔除
        m = route_re.search(stripped)
        if not m:
            continue
        method = m.group(1).upper()
        path = re.sub(r":([A-Za-z_]\w*)", r"{\1}", m.group(2))
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        params = [{"name": p, "in": "path", "required": True, "schema": {}}
                  for p in re.findall(r"\{(\w+)\}", path)]
        # REST 语义：POST 创建默认 201（其余 200）——724 真机实锤：go_route 端点
        # 恒 200 会让 POST 契约断言自错（201 实际=契约断言 200 的假失败）
        success = 201 if method == "POST" else 200
        out.append({"method": method, "path": path, "operation_id": None,
                    "params": params, "request_schema": None,
                    "response_schema": None, "source": "go_route",
                    "success_code": success, "documented_codes": []})
    # HandleFunc 形态（net-http）：方法=GET（语义为全方法注册，契约轨按 GET 试探）
    hf_re = re.compile(r'\bhttp\.HandleFunc\s*\(\s*"(/[^"]*)"')
    for ln in (text or "").splitlines():
        stripped = ln.strip()
        if stripped.startswith("//"):
            continue
        m = hf_re.search(stripped)
        if m and ("GET", m.group(1)) not in seen:
            seen.add(("GET", m.group(1)))
            out.append({"method": "GET", "path": m.group(1), "operation_id": None,
                        "params": [], "request_schema": None,
                        "response_schema": None, "source": "go_route",
                        "success_code": 200, "documented_codes": []})
    out.sort(key=lambda e: (e["path"], e["method"]))
    return out




def parse_swag_annotations(text: str) -> list[dict]:
    """swag 注释提取（任务卡-F D4 第①级升级，2026-11-12，确定性零 LLM）：
    gin 项目惯用 swag 注释块（@Router/@Param/@Success）——比 go_route 多出
    request/response schema 骨架（x-swag-ref 类型引用，字段级解析超 v1 如实标注），
    在 probe 降级链中先于 go_route（schema 更全优先）。
    注释形态：// @Router /albums/{id} [delete] / @Param id path string true "..."
    / @Param request body album.Req true "..." / @Success 200 {object} album.Album
    """
    router_re = re.compile(r"@Router\s+(\S+)\s+\[(\w+)\]")
    param_re = re.compile(
        r'@Param\s+(\w+)\s+(\w+)\s+([\w.\[\]]+)\s+(\w+)(?:\s+"([^"]*)")?')
    success_re = re.compile(r"@Success\s+(\d{3})\s+(?:\{object\}\s+([\w.]+))?")
    out: list[dict] = []
    cur: dict | None = None
    for ln in (text or "").splitlines():
        stripped = ln.strip()
        if not stripped.startswith("//"):
            continue
        if m := router_re.search(stripped):
            if cur:
                out.append(cur)
            cur = {"method": m.group(2).upper(),
                   "path": re.sub(r":([A-Za-z_]\w*)", "{\1}", m.group(1)),
                   "operation_id": None, "params": [],
                   "request_schema": None, "response_schema": None,
                   "source": "go_swag", "success_code": 200,
                   "documented_codes": []}
            continue
        if cur is None:
            continue
        if m := param_re.search(stripped):
            where = m.group(2)
            if where == "body":
                cur["request_schema"] = {"type": "object",
                                          "x-swag-ref": m.group(3),
                                          "properties": {}}
            elif where in ("path", "query"):
                cur["params"].append({"name": m.group(1), "in": where,
                                       "required": m.group(4) == "true",
                                       "schema": {"type": m.group(3)}})
        if m := success_re.search(stripped):
            cur["success_code"] = int(m.group(1))
            if m.group(2):
                cur["response_schema"] = {"type": "object",
                                           "x-swag-ref": m.group(2),
                                           "properties": {}}
    if cur:
        out.append(cur)
    seen: set = set()
    uniq = []
    for e in out:
        key = (e["method"], e["path"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq
