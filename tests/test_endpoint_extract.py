"""L2 端点提取单测：OpenAPI 解析、$ref 展开、D4 降级链（spec 有/坏/无）。"""
import json
from pathlib import Path

from test_agent.skills.endpoint_extract import parse_openapi, probe_endpoint_source, resolve_refs

COMPONENTS = {
    "components": {"schemas": {
        "Coupon": {"type": "object",
                   "required": ["code", "percent"],
                   "properties": {"code": {"type": "string"},
                                  "percent": {"type": "integer", "minimum": 1, "maximum": 100},
                                  "ref": {"$ref": "#/components/schemas/Coupon"}}},
    }}}

SPEC = {
    **COMPONENTS,
    "paths": {
        "/coupons": {
            "post": {"operationId": "create_coupon_coupons_post",
                     "requestBody": {"content": {"application/json": {
                         "schema": {"$ref": "#/components/schemas/Coupon"}}}},
                     "responses": {"201": {"content": {"application/json": {
                         "schema": {"$ref": "#/components/schemas/Coupon"}}}},
                                   "422": {"description": "validation error"}}},
        },
        "/coupons/{cid}": {
            "get": {"operationId": "read_coupon_coupons__cid__get",
                    "parameters": [{"name": "cid", "in": "path", "required": True,
                                    "schema": {"type": "integer"}}],
                    "responses": {"200": {"content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/Coupon"}}}}}},
            "parameters": [{"name": "ver", "in": "query", "required": False,
                            "schema": {"type": "string"}}],
        },
    },
}


def test_parse_openapi_endpoints_params_and_schemas():
    eps = parse_openapi(SPEC)
    assert {(e["method"], e["path"]) for e in eps} == {("POST", "/coupons"), ("GET", "/coupons/{cid}")}
    post = next(e for e in eps if e["method"] == "POST")
    assert post["request_schema"]["type"] == "object"          # $ref 已展开
    assert post["response_schema"]["required"] == ["code", "percent"]
    get = next(e for e in eps if e["method"] == "GET")
    names = [p["name"] for p in get["params"]]
    assert "cid" in names and "ver" in names                   # 级参数与操作参数合并


def test_resolve_refs_recursive_and_depth_guard():
    comps = {"schemas": {"A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
                         "B": {"type": "string"}}}
    out = resolve_refs({"$ref": "#/components/schemas/A"}, comps)
    assert out["properties"]["b"] == {"type": "string"}
    loop = {"schemas": {"X": {"$ref": "#/components/schemas/X"}}}
    assert resolve_refs({"$ref": "#/components/schemas/X"}, loop)["$ref"].endswith("X")  # 环不炸


def test_probe_finds_spec_file(tmp_path: Path):
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC), encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert how == "spec:openapi.json" and len(eps) == 2


def test_probe_honest_degradation(tmp_path: Path):
    eps, how = probe_endpoint_source(tmp_path)          # 无任何 spec
    assert eps is None and how == "no_spec"
    (tmp_path / "openapi.json").write_text("{不是 JSON", encoding="utf-8")
    eps2, how2 = probe_endpoint_source(tmp_path)        # 坏 spec → 诚实降级不炸
    assert eps2 is None and how2 == "no_spec"
