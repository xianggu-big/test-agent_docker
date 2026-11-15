# -*- coding: utf-8 -*-
"""多语言契约轨渲染（任务卡-F 阶段 1/2/3 收口）：JS supertest / Java HttpClient /
PHP curl 三形态——同一 endpoints 数据源（cg.negative_variants/example_from_schema
确定性数据），只换 HTTP 客户端调用行与断言行（渲染层零 LLM）。
真机验收需额度（LLM 需求断言层可选）；渲染层+单测零额度交付。
纪律：路径参数端点跳过（顺序场景轨负责，与 render_contract_module 同口径）。
"""
import json

from test_agent.skills.contract_multilang import (render_contract_java,
                                                  render_contract_js,
                                                  render_contract_php)

_EPS = [
    {"method": "GET", "path": "/books", "params": [],
     "request_schema": None,
     "response_schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
     "success_code": 200},
    {"method": "POST", "path": "/books", "params": [],
     "request_schema": {"type": "object", "required": ["title"],
                        "properties": {"title": {"type": "string", "example": "x"}}},
     "response_schema": None, "success_code": 201},
    {"method": "DELETE", "path": "/books/{id}",
     "params": [{"name": "id", "in": "path", "required": True, "schema": {}}],
     "request_schema": None, "response_schema": None, "success_code": 200},
]


def test_js_渲染_状态码与payload():
    code, cases, skipped = render_contract_js(_EPS, "http://127.0.0.1:8080")
    assert len(cases) == 2                               # DELETE 路径参数→跳过
    assert 'request(BASE).get("/books")' in code
    assert 'request(BASE).post("/books").send(payload)' in code
    assert "expect(res.status).toBe(201);" in code       # POST 201 契约
    assert "res.body.id" in code                         # schema 首字段抽查
    assert skipped == ["DELETE /books/{id}"]


def test_js_渲染_语法可编译():
    # 渲染产物是合法 JS（括号/引号平衡——用 node 不可用时的结构断言：首尾完整）
    code, _, _ = render_contract_js(_EPS, "http://127.0.0.1:8080")
    assert code.count("describe(") == 2 and code.rstrip().endswith("});")


def test_java_渲染_JUnit5_HttpClient():
    code, cases, skipped = render_contract_java(_EPS, "http://127.0.0.1:8080")
    assert "@Test" in code and "HttpClient.newHttpClient()" in code
    assert "assertEquals(201, res.statusCode()" in code   # POST 201 契约
    assert len(cases) == 2 and skipped == ["DELETE /books/{id}"]
    # 类壳完整（java 类渲染纪律：方法必须在 class 内）
    assert code.startswith("import ") and code.rstrip().endswith("}")


def test_php_渲染_phpunit类壳():
    code, cases, skipped = render_contract_php(_EPS, "http://127.0.0.1:8080")
    assert code.startswith("<?php")                       # <?php 必须首行（php83 纪律）
    assert "CURLOPT_CUSTOMREQUEST" in code                # curl 三件套
    assert "assertSame(201, $code" in code                # POST 201 契约
    assert len(cases) == 2 and skipped == ["DELETE /books/{id}"]


def test_三语言_状态码语义一致():
    # 同一 endpoints 数据源下三语言的成功码断言一致（POST 201/GET 200——跨语言治理）
    outs = [render_contract_js(_EPS, "b")[1],
            render_contract_java(_EPS, "b")[1],
            render_contract_php(_EPS, "b")[1]]
    expects = {tuple(sorted(c["expected"] for c in cs)) for cs in outs}
    assert len(expects) == 1                              # 三语言期望完全一致


def test_全部路径参数_全跳过():
    eps = [{"method": "DELETE", "path": "/books/{id}",
            "params": [{"name": "id", "in": "path", "required": True, "schema": {}}],
            "request_schema": None, "response_schema": None, "success_code": 200}]
    for fn in (render_contract_js, render_contract_java, render_contract_php):
        code, cases, skipped = fn(eps, "b")
        assert cases == [] and len(skipped) == 1          # 全跳过如实留痕


def test_java_渲染_PATCH_不被改成_PUT():
    """JDK11 HttpRequest.Builder.method() 直发任意方法；旧映射把 PATCH 静默改成 PUT
    →PATCH 端点契约用例必现 404/405 假"契约违背"（2026-11-15 闲时找bug）。"""
    eps = [{"method": "PATCH", "path": "/items/1", "params": [], "request_schema": None,
            "success_code": 200}]
    code, cases, skipped = render_contract_java(eps, "http://127.0.0.1:8080")
    assert 'method("PATCH"' in code, "PATCH 必须原样发 PATCH"
    assert 'method("PUT"' not in code, "PATCH 被静默改成 PUT=语义篡改"


def test_三语言_路径参数端点跳过_含required漏写():
    """spec 漏写 required 的 path 参数（OpenAPI 隐含 required）——提取层修正后
    渲染层跳过逻辑必须命中（旧实现 required=False 不跳过→字面 {id} URL 假失败）。"""
    from test_agent.skills.endpoint_extract import parse_openapi
    spec = {"paths": {"/users/{id}": {"get": {
        "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "ok"}}}}}}
    eps = parse_openapi(spec)
    for fn in (render_contract_js, render_contract_java, render_contract_php):
        _code, _cases, skipped = fn(eps, "http://127.0.0.1:8080")
        assert skipped and "/users/{id}" in skipped[0], f"{fn.__name__} 必须跳过路径参数端点"
