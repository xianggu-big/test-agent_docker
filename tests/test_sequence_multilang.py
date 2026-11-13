# -*- coding: utf-8 -*-
"""C5 场景轨三语言渲染单测（2026-11-13）：JS supertest / Java HttpClient / PHP curl——
同一 scenarios+happy 数据源（sequence_scenarios 配对形态 + happy payload），只换
HTTP 客户端调用与 JSON 读取方式；语义与 render_sequence_module（Python 金标准）
对齐：创建状态码 + id 存在性（需求断言[需求明示]）+ 读回状态码 + 标量字段读回一致。

背景（未提交草稿的三处硬伤，本组单测钉死）：JS/Java 的 ${id} 占位写在普通字符串里
不插值；PHP 缺 BASE 常量 + payload 用 JSON 花括号（C4 实锤的 PHP Parse error 坑）。
"""
import re

from test_agent.skills.callgraph import sequence_scenarios
from test_agent.skills.contract_multilang import (render_sequence_java,
                                                  render_sequence_js,
                                                  render_sequence_php)


def _endpoints():
    return [
        {"method": "POST", "path": "/books", "params": [], "request_schema": None,
         "response_schema": None, "success_code": 201},
        {"method": "GET", "path": "/books/{id}", "params": [], "request_schema": None,
         "response_schema": None, "success_code": 200},
        {"method": "GET", "path": "/books", "params": [], "request_schema": None,
         "response_schema": None, "success_code": 200},
    ]


def _fixtures():
    eps = _endpoints()
    return eps, sequence_scenarios(eps), {"POST /books": {"title": "scene-book"}}


# ---------- JS supertest ----------

def test_js_模板字面量真插值():
    # 草稿硬伤回归：${id} 必须在反引号模板字面量里，不得残留在普通双引号字符串
    _, scs, happy = _fixtures()
    code, cases = render_sequence_js(scs, happy, "http://127.0.0.1:8080")
    assert "request(BASE).get(`/books/${id}`)" in code
    assert '"/books/${id}"' not in code and '"/books/{id}"' not in code
    assert "expect(created.status).toBe(201);" in code      # 创建状态码（接口契约）
    assert "expect(res.status).toBe(200);" in code          # 读回状态码（接口契约）
    assert 'created.body["id"] ?? created.body.id' in code  # id 存在性（需求明示）
    assert 'expect(res.body["title"]).toBe(payload["title"]);' in code
    assert len(cases) == 1 and cases[0]["expected_basis"] == "需求明示"


# ---------- Java HttpClient ----------

def test_java_url拼接与jsonField():
    # 草稿硬伤回归：URL 必须是 BASE + "/books/" + id 字符串拼接，不得出现 ${id} 字面量
    _, scs, happy = _fixtures()
    code, cases = render_sequence_java(scs, happy, "http://127.0.0.1:8000")
    assert 'URI.create(BASE + "/books/" + id)' in code
    assert "${id}" not in code
    assert "String id = jsonField(createdBody, \"id\");" in code
    assert "assertEquals(201, created.statusCode()" in code
    # Java 字符串字面量里 \d 是非法转义——正则必须以 \\d 形态进文件（javac 可编译）
    assert "-?\\\\d+(?:\\\\.\\\\d+)?" in code
    assert "assertEquals(jsonField(createdBody, \"title\"), jsonField(res.body(), \"title\")" in code
    assert len(cases) == 1


def test_java_jsonField正则语义():
    # jsonField 所用正则（Java 转义还原后）在 Python re 下同语义验证：
    # 字符串（含转义）/数值/布尔/null 四形态；两侧同解析 → 读回一致可比
    pattern = r'"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null'
    body = '{"id": 7, "title": "a\\"b", "ok": true, "n": null}'
    m = re.search(r'"id"\s*:\s*(' + pattern + ")", body)
    assert m.group(1) == "7"
    m = re.search(r'"title"\s*:\s*(' + pattern + ")", body)
    assert m.group(1) == '"a\\"b"'          # 带转义的字符串整段捕获（两侧一致可比）
    m = re.search(r'"ok"\s*:\s*(' + pattern + ")", body)
    assert m.group(1) == "true"


# ---------- PHP curl ----------

def test_php_BASE常量与数组字面量():
    # 草稿硬伤回归：BASE 常量必须存在；payload 必须是 PHP 数组字面量（JSON 花括号=Parse error）
    _, scs, happy = _fixtures()
    code, cases = render_sequence_php(scs, happy, "http://127.0.0.1:8000")
    assert code.startswith("<?php")
    assert 'const BASE = "http://127.0.0.1:8000";' in code
    assert '["title" => "scene-book"]' in code
    assert '"title": "scene-book"' not in code     # JSON 对象形态不得出现
    assert "str_replace(\"{id}\", (string)$id, \"/books/{id}\")" in code
    assert "$this->assertSame(201, $code," in code
    assert len(cases) == 1


# ---------- 跨语言治理 ----------

def test_三语言_场景期望一致():
    # 同一 scenarios+happy 数据源下三语言的期望完全一致（跨语言治理，与 C4 契约轨同口径）
    _, scs, happy = _fixtures()
    outs = [render_sequence_js(scs, happy, "b")[1],
            render_sequence_java(scs, happy, "b")[1],
            render_sequence_php(scs, happy, "b")[1]]
    expects = {tuple(sorted(c["expected"] for c in cs)) for cs in outs}
    bases = {c["expected_basis"] for cs in outs for c in cs}
    assert len(expects) == 1 and bases == {"需求明示"}


def test_无happy创建体_全跳过():
    _, scs, _ = _fixtures()
    for fn in (render_sequence_js, render_sequence_java, render_sequence_php):
        code, cases = fn(scs, {}, "b")
        assert cases == []                       # 无合法创建体 → 诚实跳过（同 Python 口径）


def test_id_param非id_回退链():
    eps = _endpoints()
    eps[1] = dict(eps[1], path="/books/{qid}")   # sequence_scenarios 的 id_param 取自占位符，恒一致
    scs = [{"create": eps[0], "read": eps[1], "id_param": "qid"}]
    happy = {"POST /books": {"title": "x"}}
    js, _ = render_sequence_js(scs, happy, "b")
    assert 'created.body["qid"] ?? created.body.id' in js
    ja, _ = render_sequence_java(scs, happy, "b")
    assert 'jsonField(createdBody, "qid")' in ja
    assert 'if (id == null) id = jsonField(createdBody, "id");' in ja
    ph, _ = render_sequence_php(scs, happy, "b")
    assert "$created[\"qid\"] ?? $created['id'] ?? null" in ph


def test_读回路径无占位_诚实跳过():
    eps = _endpoints()
    eps[1] = dict(eps[1], path="/books/latest")   # GET 无 {id} 占位 → 场景不成立
    scs = sequence_scenarios(eps)
    happy = {"POST /books": {"title": "x"}}
    for fn in (render_sequence_js, render_sequence_java, render_sequence_php):
        _, cases = fn(scs, happy, "b")
        assert cases == []                       # 如实跳过，不渲染断不了言的残废用例
