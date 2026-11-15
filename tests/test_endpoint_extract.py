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


# ---------- swag 注释提取（D4 第①级升级，2026-11-12） ----------

def test_parse_swag_annotations_完整块():
    from test_agent.skills.endpoint_extract import parse_swag_annotations
    text = """// ListAlbums
// @Router /albums [get]
// @Success 200 {object} album.AlbumList
func listAlbums() {}

// DeleteAlbum
// @Router /albums/{id} [delete]
// @Param id path string true "Album ID"
// @Success 200 {object} album.Empty
func deleteAlbum() {}

// CreateAlbum
// @Router /albums [post]
// @Param request body album.AlbumCreateRequest true "payload"
// @Success 201 {object} album.Album
func createAlbum() {}
"""
    eps = parse_swag_annotations(text)
    assert len(eps) == 3
    by_route = {(e["method"], e["path"]): e for e in eps}
    assert by_route[("DELETE", "/albums/{id}")]["params"][0]["name"] == "id"
    assert by_route[("POST", "/albums")]["request_schema"]["x-swag-ref"] == \
        "album.AlbumCreateRequest"
    assert by_route[("POST", "/albums")]["success_code"] == 201   # @Success 优先
    assert by_route[("GET", "/albums")]["response_schema"]["x-swag-ref"] == "album.AlbumList"
    assert all(e["source"] == "go_swag" for e in eps)


def test_swag_probe链_swag优先于go_route(tmp_path):
    from test_agent.skills.endpoint_extract import probe_endpoint_source
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "main.go").write_text(
        "// @Router /albums [get]\n"
        "// @Success 200 {object} album.AlbumList\n"
        "func list() {}\n"
        'r.GET("/albums", list)\n', encoding="utf-8")
    eps, how = probe_endpoint_source(ws)
    assert how == "go_swag"                       # swag schema 更全优先
    assert eps[0]["response_schema"]["x-swag-ref"] == "album.AlbumList"


# ---------- swag 注释提取（D4 第①级升级，2026-11-12） ----------

def test_parse_swag_annotations_完整块():
    from test_agent.skills.endpoint_extract import parse_swag_annotations
    text = "\n".join([
        "// ListAlbums",
        "// @Router /albums [get]",
        '// @Success 200 {object} album.AlbumList',
        "func listAlbums() {}",
        "// DeleteAlbum",
        "// @Router /albums/{id} [delete]",
        '// @Param id path string true "Album ID"',
        "func deleteAlbum() {}",
        "// CreateAlbum",
        "// @Router /albums [post]",
        '// @Param request body album.AlbumCreateRequest true "payload"',
        '// @Success 201 {object} album.Album',
        "func createAlbum() {}",
    ])
    eps = parse_swag_annotations(text)
    assert len(eps) == 3
    by_route = {(e["method"], e["path"]): e for e in eps}
    assert by_route[("DELETE", "/albums/{id}")]["params"][0]["name"] == "id"
    assert by_route[("POST", "/albums")]["request_schema"]["x-swag-ref"] == \
        "album.AlbumCreateRequest"
    assert by_route[("POST", "/albums")]["success_code"] == 201   # @Success 优先
    assert by_route[("GET", "/albums")]["response_schema"]["x-swag-ref"] == "album.AlbumList"
    assert all(e["source"] == "go_swag" for e in eps)


def test_swag_probe链_swag优先于go_route(tmp_path):
    from test_agent.skills.endpoint_extract import probe_endpoint_source
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "main.go").write_text(
        "// @Router /albums [get]\n"
        "// @Success 200 {object} album.AlbumList\n"
        "func list() {}\n"
        'r.GET("/albums", list)\n', encoding="utf-8")
    eps, how = probe_endpoint_source(ws)
    assert how == "go_swag"                       # swag schema 更全优先
    assert eps[0]["response_schema"]["x-swag-ref"] == "album.AlbumList"


# ---------- 2026-11-15 闲时找bug 第一轮：resolve_refs 递归缺口 + path 参数隐含 required ----------

def test_resolve_refs_descends_lists_allof():
    """allOf/anyOf/oneOf/items 是 list——旧实现 list 在函数首行 not isinstance(dict) 直接
    原样返回，组合式 schema 内 $ref 永不展开 → 渲染物拿裸 $ref 进 jsonschema.validate
    假 ERROR（docstring 自称"生成物自包含"落空）。"""
    comps = {"schemas": {"Pet": {"type": "object",
                                 "properties": {"id": {"type": "integer"}}}}}
    out = resolve_refs({"allOf": [{"$ref": "#/components/schemas/Pet"},
                                  {"type": "object"}]}, comps)
    assert out["allOf"][0].get("properties", {}).get("id"), "list 内 $ref 必须被展开"


def test_parse_openapi_swagger2_definitions_resolved():
    """Swagger 2 的 $ref 走 #/definitions/（parse_openapi docstring 声称支持 Swagger 2）。"""
    spec = {
        "swagger": "2.0",
        "definitions": {"Pet": {"type": "object", "properties": {"id": {"type": "integer"}}}},
        "paths": {"/pets": {"post": {
            "parameters": [{"name": "body", "in": "body",
                            "schema": {"$ref": "#/definitions/Pet"}}],
            "responses": {"200": {"description": "ok"}}}}},
    }
    eps = parse_openapi(spec)
    assert eps and eps[0]["params"][0]["schema"].get("properties", {}).get("id"), \
        "#/definitions/ 引用必须被展开（自包含承诺）"


def test_parse_openapi_path_param_implicitly_required():
    """OpenAPI 规范：in=path 参数隐含 required=true（大量 spec 漏写）。旧实现
    bool(p.get("required"))=False → 渲染层"跳过路径参数端点"失效 → 生成字面
    /users/{id} URL 必假失败。"""
    spec = {
        "paths": {"/users/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok"}}}}},
    }
    eps = parse_openapi(spec)
    assert eps[0]["params"][0]["required"] is True, "path 参数必须按规范视为 required"
