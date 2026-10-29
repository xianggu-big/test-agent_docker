"""L2 调用链单测：调用图收集（同模块/跨模块）、operationId 反查 handler、顺序场景配对。"""
from pathlib import Path

from test_agent.skills.callgraph import (build_call_edges, handler_edges,
                                         handler_of, sequence_scenarios)

WS = None  # 由 fixture 写入


def _write_project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(
        "from service import calc_discount\n"
        "from fastapi import FastAPI\n"
        "api = FastAPI()\n"
        "def create_coupon(c: dict):\n"
        "    return {'id': 1, 'total': calc_discount(c)}\n"
        "def read_coupon(cid: int):\n"
        "    return {'id': cid}\n", encoding="utf-8")
    (tmp_path / "service.py").write_text(
        "def calc_discount(c):\n"
        "    return c['percent']\n"
        "def _unused():\n"
        "    return 0\n", encoding="utf-8")
    return tmp_path


def test_call_edges_same_and_cross_module(tmp_path: Path):
    ws = _write_project(tmp_path)
    g = build_call_edges(ws)
    assert g["app.create_coupon"] == ["service.calc_discount"]
    assert "app.read_coupon" not in g or g["app.read_coupon"] == []   # 无调用=空或不出现
    assert "service._unused" not in g                                  # 无调用者不出现为 key


def test_handler_lookup_via_operation_id():
    funcs = {"app.create_coupon": None, "app.read_coupon": None}
    ep = {"method": "POST", "path": "/coupons",
          "operation_id": "create_coupon_coupons_post"}
    assert handler_of(ep, funcs) == "app.create_coupon"
    assert handler_of({"method": "GET", "path": "/x", "operation_id": "nope_x_get"}, funcs) is None
    assert handler_of({"method": "GET", "path": "/x", "operation_id": None}, funcs) is None


def test_handler_edges_depth_walk():
    graph = {"app.create_coupon": ["service.calc_discount"],
             "service.calc_discount": ["service._helper"],
             "service._helper": []}
    ep = {"method": "POST", "path": "/coupons", "operation_id": "create_coupon_coupons_post"}
    edges = handler_edges([ep], graph)
    routes = [(c, ce, d) for c, ce, d in edges if c.startswith("route:")]
    assert ("route:POST /coupons", "app.create_coupon", 1) in routes
    assert ("app.create_coupon", "service.calc_discount", 2) in edges
    assert ("service.calc_discount", "service._helper", 3) in edges
    assert not any(d > 3 for _, _, d in edges)                        # 深度上限


def test_sequence_scenarios_create_then_read():
    eps = [
        {"method": "POST", "path": "/coupons", "operation_id": "create_coupon_coupons_post"},
        {"method": "GET", "path": "/coupons/{cid}", "operation_id": "read_coupon_coupons__cid__get"},
        {"method": "GET", "path": "/coupons", "operation_id": "list_coupons_coupons_get"},
        {"method": "POST", "path": "/orders", "operation_id": "create_order_orders_post"},
    ]
    scenes = sequence_scenarios(eps)
    assert len(scenes) == 1
    s = scenes[0]
    assert s["create"]["path"] == "/coupons" and s["read"]["path"] == "/coupons/{cid}"
    assert s["id_param"] == "cid"                                     # /coupons 列表接口不配对
