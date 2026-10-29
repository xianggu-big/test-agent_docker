"""L2 端点提取（D4 降级链）——契约黑盒轨的输入，纯确定性、零 LLM 零网络。

降级链（蓝图 §3.2/§11-D4）：
  ① 仓库自带 OpenAPI/Swagger spec（JSON）→ 全量解析：路径/方法/参数/请求体 schema/2xx 响应 schema
  ② 框架路由 AST 提取（Flask/FastAPI 无 spec 场景）→ P2（本轮占位不入链）
  ③ 无路由特征 → 诚实降级：返回 None，由调用方如实上报（绝不硬造端点）

$ref 处理：resolve_refs 确定性展开 components/schemas 引用（深度限 12 防循环），生成物
自包含——契约校验侧（jsonschema.validate）不再依赖 spec 上下文。
"""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
_SPEC_NAMES = ("openapi.json", "swagger.json")
_MAX_REF_DEPTH = 12


def resolve_refs(schema, components: dict, depth: int = 0):
    """确定性展开 schema 内的 $ref（components/schemas/Name）；超深或环 → 原样返回（不炸）。"""
    if depth > _MAX_REF_DEPTH or not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        target = (components or {}).get("schemas", {}).get(name)
        if isinstance(target, dict):
            return resolve_refs(target, components, depth + 1)
        return schema
    return {k: resolve_refs(v, components, depth + 1) if isinstance(v, (dict, list)) else v
            for k, v in schema.items()}


def parse_openapi(spec: dict) -> list[dict]:
    """OpenAPI 3.x/Swagger 2 端点清单。只收 5 个 HTTP 方法；2xx 响应 schema 作契约 oracle。
    paths 非字典 / 无 JSON 响应体（如 204）→ schema 置空但 success_code 如实取文档值。"""
    components = spec.get("components") or {}
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
                params.append({"name": p.get("name"), "in": p.get("in"),
                               "required": bool(p.get("required")),
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
    return None, "no_spec"
