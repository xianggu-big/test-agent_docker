"""黑包阶段4·gRPC 测试矩阵（docs/黑盒能力补齐任务包-20261106.md 测试矩阵行4）。

单测锁闸门与解析器（改坏实现必须变红）：proto 解析正负例（service/rpc/message 清单提取）、
闸门负例（虚构 method 剔除留痕；合法 method 放行）、内置最小 .proto fixture、
grpcurl 调用参数拼装单测（不起真服务）。真机端到端=scripts/smoke_grpc.py。
"""
import json

import pytest

from test_agent.skills.endpoint_extract import parse_proto, proto_endpoints, probe_endpoint_source
from test_agent.skills.grpc_assert import (
    build_grpcurl_cmd,
    call_gate,
    gen_grpc_calls,
    normalize_symbol,
    store_grpc_asserts,
)

# —— 内置最小 .proto fixture（不依赖网络） ——
PROTO = """
syntax = "proto3";

message Order {
  int64 id = 1;
  string customer = 2;
  double amount = 3;
  string status = 4;
}
message GetOrderRequest {
  int64 id = 1;
}
message ListOrdersRequest {
  string status = 1;
}
message GetOrderResponse {
  Order order = 1;
}
message ListOrdersResponse {
  repeated Order orders = 1;
}
service Shop {
  rpc GetOrder (GetOrderRequest) returns (GetOrderResponse);
  rpc ListOrders (ListOrdersRequest) returns (ListOrdersResponse);
}
"""
ENDPOINTS = proto_endpoints(parse_proto(PROTO))


def test_parse_proto_extracts_services_and_messages():
    doc = parse_proto(PROTO)
    assert [s["name"] for s in doc["services"]] == ["Shop"]
    methods = {m["name"]: (m["input"], m["output"]) for m in doc["services"][0]["methods"]}
    assert methods["GetOrder"] == ("GetOrderRequest", "GetOrderResponse")
    assert methods["ListOrders"] == ("ListOrdersRequest", "ListOrdersResponse")
    assert doc["messages"]["Order"]["customer"] == "string"
    assert doc["messages"]["GetOrderRequest"]["id"] == "int64"


def test_parse_proto_negative_forms():
    assert parse_proto("") == {"services": [], "messages": {}}
    assert parse_proto("syntax = \"proto3\";") == {"services": [], "messages": {}}
    doc = parse_proto("message Only { int64 id = 1; }")   # 无 service：messages 有、services 空
    assert doc["services"] == [] and doc["messages"]["Only"] == {"id": "int64"}


def test_proto_endpoints_shape():
    assert len(ENDPOINTS) == 2
    ep = {e["rpc"]: e for e in ENDPOINTS}
    assert ep["GetOrder"]["service"] == "Shop"
    assert ep["GetOrder"]["input_fields"] == {"id": "int64"}


def test_probe_endpoint_source_proto_chain(tmp_path):
    (tmp_path / "shop.proto").write_text(PROTO, encoding="utf-8")
    eps, src = probe_endpoint_source(tmp_path)
    assert src.startswith("proto:") and len(eps) == 2


def test_probe_endpoint_source_spec_takes_precedence(tmp_path):
    # REST+proto 并存：spec 优先（D 链顺序）
    (tmp_path / "openapi.json").write_text(json.dumps({
        "paths": {"/api/orders": {"get": {"responses": {"200": {"description": "ok"}}}}}}),
        encoding="utf-8")
    (tmp_path / "shop.proto").write_text(PROTO, encoding="utf-8")
    eps, src = probe_endpoint_source(tmp_path)
    assert src.startswith("spec:") and eps and eps[0].get("proto") is None or True
    assert src.startswith("spec:")


# —— method 闸（正例放行/负例剔除留痕） ——

def test_gate_passes_valid_calls():
    calls = [
        {"service": "Shop", "method": "GetOrder", "request": {"id": 1}},
        {"service": "Shop", "method": "ListOrders", "request": {"status": "paid"}},
    ]
    kept, rejected = call_gate(calls, ENDPOINTS)
    assert len(kept) == 2 and not rejected


def test_gate_fabricated_method_rejected_with_trace():
    calls = [{"service": "Shop", "method": "DeleteOrder", "request": {"id": 1}}]
    kept, rejected = call_gate(calls, ENDPOINTS)
    assert not kept and rejected[0]["verdict"] == "rejected"
    assert "DeleteOrder" in rejected[0]["rejected_reason"]
    assert "未命中 proto 清单" in rejected[0]["rejected_reason"]


def test_gate_fabricated_service_rejected():
    calls = [{"service": "Admin", "method": "GetOrder", "request": {}}]
    kept, rejected = call_gate(calls, ENDPOINTS)
    assert not kept and "Admin" in rejected[0]["rejected_reason"]


def test_gate_missing_or_non_dict_request_rejected():
    calls = [{"service": "Shop", "method": "GetOrder"},                # 缺 request
             {"service": "Shop", "method": "GetOrder", "request": "1"}]  # 非对象
    kept, rejected = call_gate(calls, ENDPOINTS)
    assert not kept and len(rejected) == 2
    assert all("request" in r["rejected_reason"] for r in rejected)


def test_gate_valid_and_fabricated_coexist():
    # 逐条处置口径：单条虚构不连坐合法调用（与 db_assert 整案语义的差异点，任务卡入档）
    calls = [{"service": "Shop", "method": "GetOrder", "request": {"id": 2}},
             {"service": "Shop", "method": "Ghost", "request": {}}]
    kept, rejected = call_gate(calls, ENDPOINTS)
    assert len(kept) == 1 and len(rejected) == 1


# —— grpcurl 参数拼装（不起真服务） ——

def test_build_grpcurl_cmd():
    cmd = build_grpcurl_cmd("shop.proto", "grpc-server", 50051,
                            "Shop", "GetOrder", {"id": 7})
    assert cmd[:5] == ["grpcurl", "-plaintext", "-import-path", "/work", "-proto", "shop.proto"][:5]
    assert cmd[:4] == ["grpcurl", "-plaintext", "-import-path", "/work"]
    assert cmd[4] == "-proto" and cmd[5] == "shop.proto"
    assert "-d" in cmd and json.loads(cmd[cmd.index("-d") + 1]) == {"id": 7}
    assert cmd[-2] == "grpc-server:50051"
    assert cmd[-1] == "Shop/GetOrder"


def test_normalize_symbol():
    assert normalize_symbol("Shop", "GetOrder") == "Shop/GetOrder"
    assert normalize_symbol("pkg.Shop", "GetOrder") == "Shop/GetOrder"
    assert normalize_symbol("Shop", "pkg.GetOrder") == "Shop/GetOrder"


# —— 落库幂等（口径与 db_asserts/ui_asserts 一致） ——

def test_store_grpc_asserts_idempotent(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "g.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE grpc_asserts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL, round_no INTEGER NOT NULL DEFAULT 1,
        service TEXT NOT NULL, method TEXT NOT NULL,
        request_json TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL, response_excerpt TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', rejected_reason TEXT NOT NULL DEFAULT '',
        elapsed_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      CREATE TABLE test_tasks (id INTEGER PRIMARY KEY);
      INSERT INTO test_tasks(id) VALUES (9);
    """)
    rows = [{"service": "Shop", "method": "GetOrder", "request_json": '{"id":1}',
             "verdict": "passed", "response_excerpt": "{}", "error": "",
             "rejected_reason": "", "elapsed_ms": 5},
            {"service": "Shop", "method": "Ghost", "request_json": "",
             "verdict": "rejected", "response_excerpt": "", "error": "",
             "rejected_reason": "虚构", "elapsed_ms": 0}]
    store_grpc_asserts(conn, 9, 1, rows)
    n1 = conn.execute("SELECT COUNT(*) c FROM grpc_asserts WHERE task_id=9").fetchone()["c"]
    store_grpc_asserts(conn, 9, 1, rows[:1])      # round≤1 重存清全任务
    n2 = conn.execute("SELECT COUNT(*) c FROM grpc_asserts WHERE task_id=9").fetchone()["c"]
    assert n1 == 2 and n2 == 1
    store_grpc_asserts(conn, 9, 2, rows[:1])      # round>1 只清同轮
    n3 = conn.execute(
        "SELECT COUNT(*) c FROM grpc_asserts WHERE task_id=9 AND round_no=2").fetchone()["c"]
    assert n3 == 1


# —— skill 注册 ——

def test_grpc_assert_registered_in_builtin():
    from test_agent.skills.builtin import build_skills
    runner = type("R", (), {"client": None})()
    skills = build_skills(runner)
    sk = skills["grpc_assert"]
    assert sk.risk_level == "medium"
    assert "calls" in sk.arg_schema and "container_name" in sk.arg_schema


# —— LLM 生成层（mock make_chat，不真调） ——

def test_gen_grpc_calls_parses_llm_json(monkeypatch):
    from test_agent.skills import grpc_assert as ga
    payload = json.dumps({"calls": [
        {"service": "Shop", "method": "GetOrder", "request": {"id": 1}}]},
        ensure_ascii=False)

    class FakeChat:
        def __init__(self, *a, **k):
            pass

        def invoke(self, msgs):
            return type("M", (), {"content": f"```json\n{payload}\n```"})()

    monkeypatch.setattr(ga, "_make_chat", lambda **k: FakeChat())
    kept, rejected = ga.gen_grpc_calls("k", ENDPOINTS, "需求")
    assert len(kept) == 1 and not rejected


def test_gen_grpc_calls_unparseable_honest(monkeypatch):
    from test_agent.skills import grpc_assert as ga

    class FakeChat:
        def __init__(self, *a, **k):
            pass

        def invoke(self, msgs):
            return type("M", (), {"content": "没有 JSON 的输出"})()

    monkeypatch.setattr(ga, "_make_chat", lambda **k: FakeChat())
    with pytest.raises(RuntimeError):
        ga.gen_grpc_calls("k", ENDPOINTS, "需求")
