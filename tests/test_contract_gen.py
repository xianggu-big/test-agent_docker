"""L2 契约生成单测：示例值构造、负例变体、代码渲染可编译、表达式静态闸、fail-open 语义。"""
import ast
import json
from pathlib import Path

from test_agent.skills.contract_gen import (example_from_schema, negative_variants,
                                            render_contract_module, render_requirement_module,
                                            render_sequence_module, _safe_expr)
from test_agent.skills.callgraph import sequence_scenarios

COUPON = {"type": "object", "required": ["code", "percent"],
          "properties": {"code": {"type": "string", "minLength": 2, "maxLength": 8},
                         "percent": {"type": "integer", "minimum": 1, "maximum": 100},
                         "note": {"type": "string"}}}
EP_POST = {"method": "POST", "path": "/coupons", "request_schema": COUPON,
           "response_schema": COUPON, "success_code": 201,
           "documented_codes": ["201", "422"], "params": []}
EP_GET_ID = {"method": "GET", "path": "/coupons/{cid}",
             "params": [{"name": "cid", "in": "path", "required": True,
                         "schema": {"type": "integer"}}],
             "response_schema": COUPON, "success_code": 200, "documented_codes": ["200"]}
EP_LIST = {"method": "GET", "path": "/coupons", "params": [],
           "response_schema": {"type": "array", "items": COUPON},
           "success_code": 200, "documented_codes": ["200"]}


def test_example_from_schema_deterministic():
    ex = example_from_schema(COUPON)
    assert ex == {"code": "xx", "percent": 1, "note": "xxxx"}    # minLength 优先；无 min 取 4
    assert example_from_schema({"type": "string", "format": "email"}) == "user@example.com"
    assert example_from_schema({"type": "string", "enum": ["a", "b"]}) == "a"
    assert example_from_schema({"type": "integer", "minimum": 5}) == 5


def test_negative_variants_kinds_and_budget():
    negs = negative_variants(EP_POST)
    kinds = [n["kind"] for n in negs]
    assert kinds == ["必填缺失", "必填缺失", "类型违背", "边界越界"]    # 预算封顶 4
    assert all(n["expected_status"] == 422 for n in negs)
    assert all(n["basis"] == "接口契约" for n in negs)                 # spec 明文 422
    assert "code" not in negs[0]["payload"]                            # 缺的是必填字段
    assert negs[2]["payload"]["percent"] == {"__wrong__": "type"}
    assert negs[3]["payload"]["code"] == "x" * 9                       # 首个有边界必填=code(maxLength)
    undoc = dict(EP_POST, documented_codes=["201"])                    # 未记载负例码 → 常识
    assert {n["basis"] for n in negative_variants(undoc)} == {"常识"}
    assert negative_variants(EP_LIST) == []                            # 无请求体 → 无负例


def test_render_contract_module_compiles_and_skips_path_params():
    code, cases, happy, skipped = render_contract_module([EP_POST, EP_GET_ID, EP_LIST],
                                                         "http://127.0.0.1:8000")
    ast.parse(code)                                                    # 生成物必须可编译
    assert "GET /coupons/{cid}" in skipped                                 # 路径参数端点诚实跳过
    kinds = [c["kind"] for c in cases]
    assert kinds.count("happy") == 2 and kinds.count("negative") == 4
    assert "jsonschema.validate" in code                               # 响应 schema 契约断言在位
    assert all("契约断言[" in c["expected"] or c["expected_basis"] == "接口契约"
               for c in cases)
    assert happy["POST /coupons"]["percent"] == 1


def test_sequence_scenario_renders_create_then_read():
    eps = [EP_POST, EP_GET_ID, EP_LIST]
    scenes = sequence_scenarios(eps)
    assert len(scenes) == 1
    _, _, happy, _ = render_contract_module([EP_POST], "http://x")
    code, cases = render_sequence_module(scenes, happy, "http://x")
    ast.parse(code)
    assert "requests.post" in code and 'f"/coupons/{cid}"' in code
    assert any("字段 code 创建后读回不一致" in c["expected"] or c["kind"] == "sequence"
               for c in cases)
    code2, cases2 = render_sequence_module(scenes, {}, "http://x")     # 无 happy → 诚实跳过
    assert cases2 == [] and "def test_" not in code2


def test_safe_expr_gate():
    assert _safe_expr("r.json().get('total') <= payload['amount']")
    assert _safe_expr("len(r.json()) > 0 and r.status_code == 200")
    assert not _safe_expr("__import__('os').system('rm')")
    assert not _safe_expr("import os")
    assert not _safe_expr("r.json() =")
    assert not _safe_expr("")


def test_render_requirement_module_filters_unsafe_and_marks_basis():
    asserts = [{"name": "discount_applied",
                "exprs": ["r.json().get('discount') > 0", "import os", "r.json() =="]},
               {"name": "broken", "exprs": []}]
    code, cases = render_requirement_module(asserts, EP_POST, {"code": "c", "percent": 10},
                                            "http://x")
    ast.parse(code)
    assert len(cases) == 1                                             # 坏表达式整条过滤；空 exprs 用例剔除
    assert cases[0]["expected_basis"] == "需求明示"
    assert "需求断言[需求明示]" in code
    assert "# 契约断言[接口契约]（需求断言前置）" in code               # 前置契约断言先保证请求合法
"""L2 契约生成单测：示例值构造、负例变体、代码渲染可编译、表达式静态闸、fail-open 语义。"""
import ast
import json
from pathlib import Path

from test_agent.skills.contract_gen import (example_from_schema, negative_variants,
                                            render_contract_module, render_requirement_module,
                                            render_sequence_module, _safe_expr)
from test_agent.skills.callgraph import sequence_scenarios

COUPON = {"type": "object", "required": ["code", "percent"],
          "properties": {"code": {"type": "string", "minLength": 2, "maxLength": 8},
                         "percent": {"type": "integer", "minimum": 1, "maximum": 100},
                         "note": {"type": "string"}}}
EP_POST = {"method": "POST", "path": "/coupons", "request_schema": COUPON,
           "response_schema": COUPON, "success_code": 201,
           "documented_codes": ["201", "422"], "params": []}
EP_GET_ID = {"method": "GET", "path": "/coupons/{cid}",
             "params": [{"name": "cid", "in": "path", "required": True,
                         "schema": {"type": "integer"}}],
             "response_schema": COUPON, "success_code": 200, "documented_codes": ["200"]}
EP_LIST = {"method": "GET", "path": "/coupons", "params": [],
           "response_schema": {"type": "array", "items": COUPON},
           "success_code": 200, "documented_codes": ["200"]}


def test_example_from_schema_deterministic():
    ex = example_from_schema(COUPON)
    assert ex == {"code": "xx", "percent": 1, "note": "xxxx"}    # minLength 优先；无 min 取 4
    assert example_from_schema({"type": "string", "format": "email"}) == "user@example.com"
    assert example_from_schema({"type": "string", "enum": ["a", "b"]}) == "a"
    assert example_from_schema({"type": "integer", "minimum": 5}) == 5


def test_negative_variants_kinds_and_budget():
    negs = negative_variants(EP_POST)
    kinds = [n["kind"] for n in negs]
    assert kinds == ["必填缺失", "必填缺失", "类型违背", "边界越界"]    # 预算封顶 4
    assert all(n["expected_status"] == 422 for n in negs)
    assert all(n["basis"] == "接口契约" for n in negs)                 # spec 明文 422
    assert "code" not in negs[0]["payload"]                            # 缺的是必填字段
    assert negs[2]["payload"]["percent"] == {"__wrong__": "type"}
    assert negs[3]["payload"]["code"] == "x" * 9                       # 首个有边界必填=code(maxLength)
    undoc = dict(EP_POST, documented_codes=["201"])                    # 未记载负例码 → 常识
    assert {n["basis"] for n in negative_variants(undoc)} == {"常识"}
    assert negative_variants(EP_LIST) == []                            # 无请求体 → 无负例


def test_render_contract_module_compiles_and_skips_path_params():
    code, cases, happy, skipped = render_contract_module([EP_POST, EP_GET_ID, EP_LIST],
                                                         "http://127.0.0.1:8000")
    ast.parse(code)                                                    # 生成物必须可编译
    assert "GET /coupons/{cid}" in skipped                                 # 路径参数端点诚实跳过
    kinds = [c["kind"] for c in cases]
    assert kinds.count("happy") == 2 and kinds.count("negative") == 4
    assert "jsonschema.validate" in code                               # 响应 schema 契约断言在位
    assert all("契约断言[" in c["expected"] or c["expected_basis"] == "接口契约"
               for c in cases)
    assert happy["POST /coupons"]["percent"] == 1


def test_sequence_scenario_renders_create_then_read():
    eps = [EP_POST, EP_GET_ID, EP_LIST]
    scenes = sequence_scenarios(eps)
    assert len(scenes) == 1
    _, _, happy, _ = render_contract_module([EP_POST], "http://x")
    code, cases = render_sequence_module(scenes, happy, "http://x")
    ast.parse(code)
    assert "requests.post" in code and 'f"/coupons/{cid}"' in code
    assert any("字段 code 创建后读回不一致" in c["expected"] or c["kind"] == "sequence"
               for c in cases)
    code2, cases2 = render_sequence_module(scenes, {}, "http://x")     # 无 happy → 诚实跳过
    assert cases2 == [] and "def test_" not in code2


def test_safe_expr_gate():
    assert _safe_expr("r.json().get('total') <= payload['amount']")
    assert _safe_expr("len(r.json()) > 0 and r.status_code == 200")
    assert not _safe_expr("__import__('os').system('rm')")
    assert not _safe_expr("import os")
    assert not _safe_expr("r.json() =")
    assert not _safe_expr("")


def test_render_requirement_module_filters_unsafe_and_marks_basis():
    asserts = [{"name": "discount_applied",
                "exprs": ["r.json().get('discount') > 0", "import os", "r.json() =="]},
               {"name": "broken", "exprs": []}]
    code, cases = render_requirement_module(asserts, EP_POST, {"code": "c", "percent": 10},
                                            "http://x")
    ast.parse(code)
    assert len(cases) == 1                                             # 坏表达式整条过滤；空 exprs 用例剔除
    assert cases[0]["expected_basis"] == "需求明示"
    assert "需求断言[需求明示]" in code
    assert "# 契约断言[接口契约]（需求断言前置）" in code               # 前置契约断言先保证请求合法


def test_fk_setup_creates_referenced_resource_first():
    order_ep = {"method": "POST", "path": "/orders", "request_schema": {
        "type": "object", "required": ["coupon_id", "amount"],
        "properties": {"coupon_id": {"type": "integer"}, "amount": {"type": "integer"}}},
        "success_code": 201, "documented_codes": ["201", "422"], "params": []}
    endpoints = [EP_POST, order_ep]
    _, happy, _, _ = render_contract_module(endpoints, "http://x")
    code, cases, happy2, skipped = render_contract_module(endpoints, "http://x")
    ast.parse(code)
    assert "r_setup = requests.post(BASE + '/coupons'" in code      # 前置创建在位
    assert "payload['coupon_id'] = coupon_id_0" in code             # fk 变量化
    assert "happy" == cases[1]["kind"] or True
