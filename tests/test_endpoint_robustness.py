"""L2 提取层鲁棒性单测（T2）：D4 降级链在怪 spec 下的行为——不炸、诚实降级。"""
import json
from pathlib import Path

from test_agent.skills.endpoint_extract import parse_openapi, probe_endpoint_source


def test_spec_with_no_paths_yields_empty_and_degrades(tmp_path: Path):
    (tmp_path / "openapi.json").write_text(json.dumps({"openapi": "3.1.0", "paths": {}}),
                                           encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert eps is None and how == "no_spec"        # 有文件但无端点 → 降级试下一个来源


def test_spec_paths_not_dict_does_not_crash(tmp_path: Path):
    (tmp_path / "openapi.json").write_text(json.dumps({"paths": "不是字典"}),
                                           encoding="utf-8")
    assert parse_openapi({"paths": "x"}) == []
    eps, how = probe_endpoint_source(tmp_path)
    assert eps is None


def test_unsupported_methods_skipped(tmp_path: Path):
    spec = {"paths": {"/trace": {"trace": {"responses": {}}},   # 非 5 法
                      "/head": {"head": {"responses": {}}},
                      "/ok": {"get": {"operationId": "ok_ok_get", "responses": {}}}}}
    eps = parse_openapi(spec)
    assert [(e["method"], e["path"]) for e in eps] == [("GET", "/ok")]


def test_allof_and_missing_content_honest_empty_schemas(tmp_path: Path):
    """allOf/oneOf 不展开（P1 边界）→ schema 原样保留、不炸；无响应体 → schema None。"""
    spec = {"paths": {"/a": {"post": {
        "requestBody": {"content": {"application/json": {
            "schema": {"allOf": [{"$ref": "#/components/schemas/X"}]}}}},
        "responses": {"204": {"description": "no content"}}}}}}
    eps = parse_openapi(spec)
    assert eps[0]["request_schema"] == {"allOf": [{"$ref": "#/components/schemas/X"}]}
    assert eps[0]["response_schema"] is None
    assert eps[0]["success_code"] == 204 and eps[0]["documented_codes"] == ["204"]


def test_second_spec_source_fallback(tmp_path: Path):
    """第一个 spec 损坏 → 降级试 swagger.json（D4 链多来源兜底）。"""
    (tmp_path / "openapi.json").write_text("{坏 JSON", encoding="utf-8")
    (tmp_path / "swagger.json").write_text(json.dumps(
        {"paths": {"/ping": {"get": {"operationId": "ping_ping_get", "responses": {}}}}}),
        encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert how == "spec:swagger.json" and len(eps) == 1
