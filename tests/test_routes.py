# -*- coding: utf-8 -*-
"""多测试路线编排 期 A1（2026-11-09）：声明式注册表+确定性路由器+route_plan 落库。

核心纪律锁：preconditions 由确定性代码判定（LLM 不得越过前置条件选路线）；
每条候选路线产出 选/不选+理由（被剔除者可审计）；MTA_ROUTER 默认关。
"""
import sqlite3

import pytest

from test_agent.skills.routes import (ROUTES, evaluate_route,
                                      extract_route_features, route_plan,
                                      save_route_plan)


def test_687特征重放_纯HTML项目路由到UI幂等():
    # task 687 实锤：纯 HTML（index.html/students.html）→ language=unknown →
    # 旧链路 static_only 扫描 0 文件无出口；路由器必须给它 UI 交互幂等路线
    f = {"language": "unknown", "py": 0, "js": 0, "html": 2, "proto": 0, "sql": 0,
         "has_forms": True, "has_interactive": True, "has_http_framework": False,
         "has_package_json": False, "has_go_mod": False}
    plan = route_plan("测试一些基础功能", f)
    ids = plan["selected_ids"]
    assert "ui_interaction_idempotency" in ids          # 687 的执行出口
    assert "static_scan" in ids                          # 兜底永真
    assert "l1_unit_pytest" not in ids                   # 非 python，剔除
    # 被剔除者有理由（可审计）
    l1 = next(r for r in plan["routes"] if r["route_id"] == "l1_unit_pytest")
    assert l1["selected"] is False and "主语言" in l1["reason"]
    grpc = next(r for r in plan["routes"] if r["route_id"] == "grpc_contract")
    assert grpc["selected"] is False and "proto" in grpc["reason"]


def test_python项目多路线并选():
    f = {"language": "python", "py": 5, "js": 0, "html": 0, "proto": 0, "sql": 3,
         "has_forms": False, "has_interactive": False, "has_http_framework": True,
         "has_package_json": False, "has_go_mod": False}
    plan = route_plan("需求", f, flags={"l2": True, "l3": False, "l4": True})
    ids = plan["selected_ids"]
    assert "l1_unit_pytest" in ids and "l2_contract_http" in ids
    assert "db_assert" in ids                             # has_sql
    assert "l3_scenario" not in ids                       # 旗标关闭→剔除且记录
    l3 = next(r for r in plan["routes"] if r["route_id"] == "l3_scenario")
    assert l3["selected"] is False and "旗标" in l3["reason"]
    assert "ui_interaction_idempotency" not in ids        # 无 HTML


def test_全部路线都有判定结果_无遗漏():
    f = extract_route_features("D:/test agent_docker/data/demo/go-ver-gate")
    plan = route_plan("x", {**f, "language": "go"})
    assert {r["route_id"] for r in plan["routes"]} == {r.id for r in ROUTES}
    assert len(plan["routes"]) == 9
    for r in plan["routes"]:
        assert isinstance(r["selected"], bool) and r["reason"]


def test_空项目仅兜底路线():
    plan = route_plan("x", {"language": "unknown", "py": 0, "js": 0, "html": 0,
                            "proto": 0, "sql": 0, "has_interactive": False,
                            "has_http_framework": False})
    assert plan["selected_ids"] == ["static_scan"]


def test_优先级排序_幂等路线先于静态():
    f = {"language": "unknown", "py": 0, "js": 0, "html": 1, "proto": 0, "sql": 0,
         "has_interactive": True, "has_http_framework": False}
    plan = route_plan("x", f)
    assert plan["selected_ids"].index("ui_interaction_idempotency") \
        < plan["selected_ids"].index("static_scan")


def test_route_plan落库_幂等重写(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE route_plan (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
        route_id TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 50,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    routes = [{"route_id": "static_scan", "selected": True, "reason": "兜底", "priority": 90},
              {"route_id": "db_assert", "selected": False, "reason": "无 SQL", "priority": 30}]
    save_route_plan(conn, 1, routes)
    save_route_plan(conn, 1, routes)                      # 幂等：同任务重写不叠加
    n = conn.execute("SELECT COUNT(*) FROM route_plan WHERE task_id=1").fetchone()[0]
    assert n == 2
    conn.close()


def test_687真实工作区特征提取():
    f = extract_route_features(r"D:\c学习数据结构\test_1_学生管理系统")
    assert f["html"] >= 1 and f["has_interactive"] is True   # 真实项目有交互元素
    plan = route_plan("测试一些基础功能", f)
    assert "ui_interaction_idempotency" in plan["selected_ids"]
