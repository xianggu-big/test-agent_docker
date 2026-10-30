"""四级 RTM 跨级溯源链单测（纯确定性，tmp 库，零 Docker/LLM）。

覆盖：端点模板归一 / 脚本级别判定 / L3 全链（L4 上游+L2+L1+代码位置）/
缺调用图诚实缺席 / RTM 缺失时文本兜底 / 不硬猜未知端点 / 任务边界 404。
"""
import json

import pytest

from test_agent.skills.traceability import (build_task_traces, build_trace_chain,
                                            route_key, script_level)
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "trace.db"
    init_db(db)
    c = get_conn(db)
    c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')")
    c.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed)"
              " VALUES(1,1,'需求',1)")
    c.commit()
    yield c
    c.close()


def _ep(conn, method, path, op=None, task=1):
    conn.execute("INSERT INTO interface_endpoints(task_id,method,path,operation_id)"
                 " VALUES(?,?,?,?)", (task, method, path, op))


def _script(conn, framework, content, task=1) -> int:
    return conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,content,content_hash)"
        " VALUES(?,?,1,?,?)", (task, framework, content, "h")).lastrowid


def _case(conn, script_id, name, steps=None, expected=None, basis=None, content="x"):
    conn.execute(
        "INSERT INTO test_cases(script_id,source,name,method,content,steps,expected,"
        "expected_basis,enabled) VALUES(?, 'ai', ?, '边界值分析', ?, ?, ?, ?, 1)",
        (script_id, name, content, steps, expected, basis))


def _scenario(conn, title, steps, status="pending_confirm", level="L3", ref=None,
              last=None, task=1) -> int:
    return conn.execute(
        "INSERT INTO scenario_cases(task_id,level,title,requirement_ref,steps,expected,"
        "last_result,status) VALUES(?,?,?,?,?,?,?,?)",
        (task, level, title, ref, json.dumps(steps, ensure_ascii=False), "场景预期",
         json.dumps(last, ensure_ascii=False) if last else None, status)).lastrowid


def _chain(scenario_id, conn, task=1):
    return build_trace_chain(conn, task, scenario_id)


def test_route_key_模板归一与不硬猜():
    eps = [{"method": "GET", "path": "/coupons/{cid}"},
           {"method": "POST", "path": "/orders"}]
    assert route_key("GET", "/coupons/1", eps) == "GET /coupons/{cid}"
    assert route_key("GET", "/coupons/{cid}", eps) == "GET /coupons/{cid}"
    assert route_key("POST", "/orders", eps) == "POST /orders"
    assert route_key("POST", "/ghost", eps) == "POST /ghost"      # 未知原样，不猜


def test_script_level_三分类():
    assert script_level("acceptance", "requests") == "L4"
    assert script_level("pytest", "import requests\nBASE='http://x'") == "L2"
    assert script_level("pytest", "from fastapi.testclient import TestClient") == "L1"
    # 回归锁：L1 用例可能 import requests 做辅助（无 BASE 端点地址），
    # 老启发式只要看到 requests 子串就误判 L2，跨级链会把单元用例当集成用例。
    assert script_level("pytest", "import requests\n# 仅用于下载 fixture\n") == "L1"


def _seed_full(conn):
    _ep(conn, "POST", "/coupons", "create_coupon_coupons_post")
    _ep(conn, "POST", "/orders", "create_order_orders_post")
    _ep(conn, "GET", "/orders/{oid}", "read_order_orders__oid__get")
    l2_script = _script(conn, "pytest", "import requests\nBASE = 'http://127.0.0.1:8000'\n")
    _case(conn, l2_script, "test_post_orders_契约黑盒", steps="POST /orders",
          expected="400", basis="接口契约")
    l1_script = _script(conn, "pytest", "from fastapi.testclient import TestClient\n")
    _case(conn, l1_script, "test_order_reuse_coupon_400_错误推测法",
          steps="创建优惠券后再次 POST /orders", expected="同一优惠券重复下单返回 400",
          basis="需求明示")
    cond = conn.execute(
        "INSERT INTO test_conditions(task_id,requirement_ref,condition,priority)"
        " VALUES(1,'同一优惠券重复下单 → 400','同一优惠券首次下单成功后，再次使用同一优惠券下单，"
        "响应状态码为 400','高')").lastrowid
    conn.execute(
        "INSERT INTO rtm_maps(task_id,source,matrix) VALUES(1,'heuristic',?)",
        (json.dumps([{"condition_id": cond, "condition": "同一优惠券重复下单",
                      "requirement_ref": "同一优惠券重复下单 → 400", "priority": "高",
                      "cases": ["test_order_reuse_coupon_400_错误推测法"],
                      "status": "covered"}], ensure_ascii=False),))
    # 上游 L4 已确认 + 无关 L4（不应被拉进链）
    _scenario(conn, "同一优惠券重复下单返回 400", [{"method": "POST", "path": "/orders"}],
              level="L4", status="confirmed_defect", ref="同一优惠券重复下单 → 400")
    _scenario(conn, "创建优惠券 code 长度边界", [{"method": "POST", "path": "/coupons"}],
              level="L4", status="confirmed_pass")
    sid = _scenario(
        conn, "同一优惠券重复下单返回 400",
        [{"method": "POST", "path": "/coupons"},
         {"method": "POST", "path": "/orders", "body": {"amount": 100}}],
        ref="同一优惠券重复下单 → 400",
        last={"title": "同一优惠券重复下单返回 400", "ok": False,
              "steps": [{"step": "POST /coupons", "status": 201, "ok": True},
                        {"step": "POST /orders", "status": 201, "ok": False}]})
    conn.execute(
        "INSERT INTO call_edges(task_id,caller,callee,depth,file,lineno) VALUES"
        "(1,'route:POST /orders','app.create_order',1,'app.py',50),"
        "(1,'app.create_order','app.calc_discount',2,'app.py',10),"
        "(1,'route:POST /coupons','app.create_coupon',1,'app.py',30)")
    for lv in ("L1", "L2", "L3"):
        conn.execute("INSERT INTO test_levels(task_id,level,status,verdict) VALUES(1,?,?,?)",
                     (lv, "done" if lv != "L2" else "failed",
                      "pass" if lv == "L1" else ("stop" if lv == "L2" else "semi_auto")))
    conn.commit()
    return sid


def test_l3_full_chain(conn):
    sid = _seed_full(conn)
    chain = _chain(sid, conn)
    levels = [n["level"] for n in chain["nodes"]]
    assert levels == ["L4", "L3", "L2", "L1", "CODE", "CODE"]
    assert chain["trigger"]["focus_route"] == "POST /orders"
    l4 = chain["nodes"][0]
    assert l4["kind"] == "acceptance_scenario" and l4["status"] == "confirmed_defect"
    assert "code 长度边界" not in l4["title"]              # 无关 L4 不拉进链
    l2 = chain["nodes"][2]
    assert l2["name"] == "test_post_orders_契约黑盒" and l2["status"] == "candidate"
    l1 = chain["nodes"][3]
    assert l1["name"] == "test_order_reuse_coupon_400_错误推测法"
    assert l1["expected_basis"] == "需求明示"
    code = [n for n in chain["nodes"] if n["level"] == "CODE"]
    assert [n["function"] for n in code] == ["app.create_order", "app.calc_discount"]
    assert code[0]["file"] == "app.py" and code[0]["lineno"] == 50
    assert code[0]["role"] == "handler" and code[1]["role"] == "callee"
    assert chain["gaps"] == []
    assert [l["level"] for l in chain["level_chain"]] == ["L1", "L2", "L3"]


def test_无调用图诚实缺席(conn):
    sid = _scenario(conn, "任意场景", [{"method": "POST", "path": "/ghost"}],
                    last={"ok": False, "steps": [{"step": "POST /ghost", "status": 500,
                                                  "ok": False}]})
    chain = _chain(sid, conn)
    assert any("无调用图" in g for g in chain["gaps"])
    assert [n["level"] for n in chain["nodes"]] == ["L3"]
    assert any("无 L1" in g for g in chain["gaps"])


def test_失败端点无调用图不回退到别的端点(conn):
    """真 bug 锁：focus 路由无 call_edges 时，老实现会 `or roots` 回退到全部端点，
    把 /orders 的代码位置冒充成 /ghost 的溯源结果。"""
    _ep(conn, "POST", "/orders")
    conn.execute(
        "INSERT INTO call_edges(task_id,caller,callee,depth,file,lineno)"
        " VALUES(1,'route:POST /orders','app.create_order',1,'app.py',50)")
    sid = _scenario(conn, "幽灵端点失败", [{"method": "POST", "path": "/ghost"}],
                    last={"ok": False, "steps": [{"step": "POST /ghost", "status": 500,
                                                  "ok": False}]})
    chain = _chain(sid, conn)
    code = [n for n in chain["nodes"] if n["level"] == "CODE"]
    assert code == []
    assert any("无该端点" in g for g in chain["gaps"])


def test_rtm缺失时L1文本兜底(conn):
    _ep(conn, "POST", "/orders")
    l1_script = _script(conn, "pytest", "from fastapi.testclient import TestClient\n")
    _case(conn, l1_script, "test_order_reuse_coupon_400_错误推测法",
          expected="同一优惠券重复下单返回 400", basis="需求明示")
    sid = _scenario(conn, "同一优惠券重复下单返回 400", [{"method": "POST", "path": "/orders"}])
    chain = _chain(sid, conn)
    l1 = [n for n in chain["nodes"] if n["level"] == "L1"]
    assert l1 and l1[0]["name"] == "test_order_reuse_coupon_400_错误推测法"
    assert "case 2-gram" in chain["l1_link_basis"]


def test_未知端点不硬猜L2(conn):
    l2_script = _script(conn, "pytest",
                        "import requests\nBASE = 'http://127.0.0.1:8000'\n")
    _case(conn, l2_script, "test_post_orders_契约黑盒", steps="POST /orders")
    _ep(conn, "POST", "/coupons")
    sid = _scenario(conn, "幽灵接口场景", [{"method": "POST", "path": "/ghost"}])
    chain = _chain(sid, conn)
    assert [n for n in chain["nodes"] if n["level"] in ("L2", "L1")] == []


def test_任务边界_跨任务场景404(conn):
    _seed_full(conn)
    conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed)"
                 " VALUES(1,1,'另一任务',1)")
    sid = _scenario(conn, "别的任务场景", [{"method": "POST", "path": "/orders"}], task=2)
    with pytest.raises(LookupError):
        build_trace_chain(conn, 1, sid)


def test_task_traces只收失败与确认缺陷(conn):
    _seed_full(conn)
    _scenario(conn, "自动通过不触发", [{"method": "POST", "path": "/orders"}],
              status="auto_pass")
    _scenario(conn, "L4 待确认不触发", [{"method": "POST", "path": "/orders"}],
              level="L4", status="pending")
    chains = build_task_traces(conn, 1)
    statuses = [c["trigger"]["status"] for c in chains]
    assert statuses.count("pending_confirm") == 1
    assert "confirmed_defect" in statuses
    assert "auto_pass" not in statuses and "pending" not in statuses


def test_l4触发不重复上游(conn):
    _ep(conn, "POST", "/orders")
    sid = _scenario(conn, "验收场景", [{"method": "POST", "path": "/orders"}],
                    level="L4", status="confirmed_defect")
    chain = _chain(sid, conn)
    assert [n["level"] for n in chain["nodes"]] == ["L4"]
    assert chain["trigger"]["level"] == "L4"


def test_场景步骤真实id归一匹配L2(conn):
    _ep(conn, "GET", "/orders/{oid}")
    l2_script = _script(conn, "pytest",
                        "import requests\nBASE = 'http://127.0.0.1:8000'\n")
    _case(conn, l2_script, "test_get_order_契约黑盒", steps="GET /orders/{oid}")
    sid = _scenario(conn, "按 id 查询订单", [{"method": "GET", "path": "/orders/1"}])
    chain = _chain(sid, conn)
    l2 = [n for n in chain["nodes"] if n["level"] == "L2"]
    assert l2 and l2[0]["name"] == "test_get_order_契约黑盒"
