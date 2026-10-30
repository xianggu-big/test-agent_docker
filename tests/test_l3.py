"""L3 系统测试半自动（D5）机制单测 —— fake-LLM，不依赖 Docker/网络。"""
import json
import os

import pytest

from test_agent import config
from test_agent.skills.scenario_gen import (apply_smoke_results, build_scenario_prompt,
                                            confirm_scenario, gen_scenarios_llm,
                                            inject_body_refs, l3_metrics,
                                            parse_scenarios, save_scenarios)

ENDPOINTS = [{"method": "POST", "path": "/users"}, {"method": "GET", "path": "/users/{id}"},
             {"method": "POST", "path": "/orders"}]

GOOD_JSON = json.dumps({"scenarios": [
    {"title": "注册后下单", "requirement_ref": "用户可注册并下单",
     "steps": [{"method": "POST", "path": "/users", "body": {"name": "a"}, "expect_status": 200},
               {"method": "POST", "path": "/orders", "body": {"user": 1}}],
     "expected": "下单成功"}]})


def _mem_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE scenario_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, level TEXT DEFAULT 'L3',
        title TEXT, requirement_ref TEXT, steps TEXT, expected TEXT, gwt TEXT,
        status TEXT DEFAULT 'pending', last_result TEXT, actual_note TEXT,
        confirmed_by INTEGER, confirmed_at TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    return conn


class TestStaticGate:
    def test_合法场景通过且method归一(self):
        valid, rej = parse_scenarios(GOOD_JSON, ENDPOINTS)
        assert len(valid) == 1 and not rej
        assert valid[0]["steps"][0]["method"] == "POST"

    def test_虚构接口整场景剔除(self):
        bad = json.dumps({"scenarios": [{"title": "坏场景", "steps": [
            {"method": "DELETE", "path": "/nowhere"}]}]})
        valid, rej = parse_scenarios(bad, ENDPOINTS)
        assert valid == [] and "不在端点清单" in rej[0]

    def test_不可解析诚实失败(self):
        valid, rej = parse_scenarios("抱歉", ENDPOINTS)
        assert valid == [] and "不可解析" in rej[0]

    def test_gen_llm调用计数与闸(self):
        valid, rej, calls = gen_scenarios_llm("需求", ENDPOINTS, lambda p: GOOD_JSON, 1)
        assert calls == 1 and len(valid) == 1
        v2, _, calls2 = gen_scenarios_llm("需求", [], lambda p: GOOD_JSON, 1)
        assert calls2 == 0 and v2 == []          # 无端点=无静态闸基准，零调用


class TestBodyRefs:
    """W2③（corpus-003 实锤）：示例 body 的字面 room_id=100 被模型照抄 → 404 假失败。"""

    def test_示例body外键给占位符(self):
        prompt = build_scenario_prompt("需求", [{
            "method": "POST", "path": "/bookings",
            "request_schema": {"required": ["room_id", "date"], "properties": {
                "room_id": {"type": "integer"}, "date": {"type": "string"}}}}])
        assert '"room_id": "{room_id}"' in prompt

    def test_正例外键注入占位符(self):
        steps = inject_body_refs([
            {"method": "POST", "path": "/rooms", "body": {"name": "R"}},
            {"method": "POST", "path": "/bookings",
             "body": {"room_id": 100, "date": "2026-11-01"}, "expect_status": 201}])
        assert steps[1]["body"]["room_id"] == "{room_id}"
        assert steps[0]["body"]["name"] == "R"

    def test_负例保持字面id(self):
        steps = inject_body_refs([
            {"method": "POST", "path": "/rooms"},
            {"method": "POST", "path": "/bookings",
             "body": {"room_id": 999}, "expect_status": 404}])
        assert steps[1]["body"]["room_id"] == 999       # 负例意图不破坏

    def test_无前置创建保持字面id(self):
        steps = inject_body_refs([
            {"method": "POST", "path": "/bookings", "body": {"room_id": 100},
             "expect_status": 201}])
        assert steps[0]["body"]["room_id"] == 100

    def test_嵌套body与既有占位符(self):
        steps = inject_body_refs([
            {"method": "POST", "path": "/coupons"},
            {"method": "POST", "path": "/orders",
             "body": {"coupon_id": "{cid}", "meta": {"coupon_id": 5}},
             "expect_status": 201}])
        assert steps[1]["body"]["coupon_id"] == "{cid}"          # 已有占位符不动
        assert steps[1]["body"]["meta"]["coupon_id"] == "{coupon_id}"


class TestSaveAndIdempotent:
    def test_重跑保留已确认_只重置pending(self):
        conn = _mem_conn()
        save_scenarios(conn, 1, [{"title": "S1", "steps": [
            {"method": "POST", "path": "/users"}]}])
        conn.execute("UPDATE scenario_cases SET status='confirmed_defect' WHERE title='S1'")
        conn.commit()
        n = save_scenarios(conn, 1, [{"title": "S1", "steps": [
            {"method": "POST", "path": "/users"}]},
            {"title": "S2", "steps": [{"method": "POST", "path": "/orders"}]}])
        assert n == 2                             # 本轮写入 2 条
        rows = conn.execute("SELECT title, status FROM scenario_cases ORDER BY id").fetchall()
        assert rows[0]["status"] == "confirmed_defect"   # 人工结论不丢
        assert rows[1]["status"] == "pending"            # S2 是新 pending


class TestSmokeAndConfirm:
    def test_冒烟分流_全过auto_失败pending(self):
        conn = _mem_conn()
        save_scenarios(conn, 1, [{"title": "S1", "steps": [{"method": "POST", "path": "/users"}]},
                                 {"title": "S2", "steps": [{"method": "POST", "path": "/orders"}]}])
        counts = apply_smoke_results(conn, 1, [
            {"title": "S1", "ok": True}, {"title": "S2", "ok": False}])
        # loop-3 起返回结构新增 duplicate_titles_manual 字段——子集断言防再次破坏
        assert counts["auto_pass"] == 1 and counts["pending_confirm"] == 1
        flags = dict(conn.execute("SELECT title, status FROM scenario_cases").fetchall())
        assert flags["S1"] == "auto_pass" and flags["S2"] == "pending_confirm"

    def test_confirm三选一回库(self):
        conn = _mem_conn()
        save_scenarios(conn, 1, [{"title": "S1", "steps": [{"method": "POST", "path": "/users"}]}])
        sid = conn.execute("SELECT id FROM scenario_cases").fetchone()["id"]
        out = confirm_scenario(conn, sid, "defect", "订单确实没落库", user_id=7)
        assert out["status"] == "confirmed_defect"
        row = conn.execute("SELECT actual_note, confirmed_by FROM scenario_cases").fetchone()
        assert "没落库" in row["actual_note"] and row["confirmed_by"] == 7

    def test_confirm非法result拒绝(self):
        conn = _mem_conn()
        save_scenarios(conn, 1, [{"title": "S1", "steps": [{"method": "POST", "path": "/users"}]}])
        sid = conn.execute("SELECT id FROM scenario_cases").fetchone()["id"]
        with pytest.raises(ValueError):
            confirm_scenario(conn, sid, "随便", None, 1)

    def test_l3_metrics分母剔pending(self):
        conn = _mem_conn()
        save_scenarios(conn, 1, [{"title": f"S{i}", "steps": [
            {"method": "POST", "path": "/users"}]} for i in range(1, 5)])
        apply_smoke_results(conn, 1, [
            {"title": "S1", "ok": True}, {"title": "S2", "ok": True},
            {"title": "S3", "ok": False}, {"title": "S4", "ok": True}])
        sid = conn.execute("SELECT id FROM scenario_cases WHERE title='S3'").fetchone()["id"]
        confirm_scenario(conn, sid, "defect", "人工确认：产品缺陷", 1)
        m = l3_metrics(conn, 1)
        # 终态：3 auto_pass + 1 confirmed_defect，pending=0 → 分母 4
        assert m["total"] == 4 and m["auto_pass"] == 3 and m["confirmed_defect"] == 1
        assert m["pending_confirm"] == 0 and m["pass_rate"] == 0.75
        assert l3_metrics(conn, 999) is None      # 无行整体缺席


def test_冒烟脚本含引用链与语法合法():
    from test_agent.skills.scenario_gen import build_smoke_script
    s = build_smoke_script([{"title": "t", "steps": [
        {"method": "POST", "path": "/coupons"},
        {"method": "GET", "path": "/coupons/{cid}"}]}], "http://x")
    assert "def sub" in s and "ctx.update" in s      # 引用链：前步响应→后步路径替换
    assert 'last_obj.get(' in s                       # {xxx_id} 兜底
    compile(s, "smoke", "exec")                       # 生成物语法合法


def test_提示词含schema摘要与负例纪律():
    from test_agent.skills.scenario_gen import build_scenario_prompt
    eps = [{"method": "POST", "path": "/coupons",
            "request_schema": {"type": "object",
                               "properties": {"code": {"type": "string"},
                                              "percent": {"type": "integer"}},
                               "required": ["code", "percent"]}}]
    p = build_scenario_prompt("需求", eps)
    assert "code:string*" in p and "percent:integer" in p   # 必填*标记与类型摘要
    assert "负例纪律" in p and "999999" in p                # 负例字面值指导
    assert "禁止用 {占位符}" in p                           # 占位符仅限真实资源引用


def test_模型漂移name键容错():
    drifted = json.dumps({"scenarios": [{"name": "漂移场景", "steps": [
        {"method": "POST", "path": "/users"}]}]})
    valid, rej = parse_scenarios(drifted, ENDPOINTS)
    assert len(valid) == 1 and valid[0]["title"] == "漂移场景" and not rej


def test_冒烟脚本body引用链():
    from test_agent.skills.scenario_gen import build_smoke_script
    s = build_smoke_script([{"title": "t", "steps": [
        {"method": "POST", "path": "/coupons", "body": {"code": "X"}},
        {"method": "POST", "path": "/orders", "body": {"coupon_id": "{cid}", "amount": 50}}]}], "http://x")
    assert "def sub_body" in s                    # body 深遍历替换（422 实锤修法）
    compile(s, "smoke", "exec")


def test_冒烟脚本引用链端到端_假服务执行(tmp_path):
    """行为级验证：生成的冒烟脚本对真实 HTTP 服务执行，
    路径 {cid} 与 body {coupon_id} 都被前一步响应的 id 替换（W2 实锤修法的行为锁）。"""
    import http.server
    import json as _json
    import subprocess
    import sys
    import threading

    received = []

    class H(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            raw = b""
            n = self.headers.get("Content-Length")
            if self.command == "POST" and n:
                raw = self.rfile.read(int(n))
            body = _json.loads(raw) if raw else None
            received.append((self.command, self.path, body))
            resp = _json.dumps({"id": 1, "code": "X"}).encode()
            code = 201 if self.command == "POST" else 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        do_POST = do_GET = _handle

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from test_agent.skills.scenario_gen import build_smoke_script
        s = build_smoke_script([{"title": "链式场景", "steps": [
            {"method": "POST", "path": "/coupons", "body": {"code": "X"}},
            {"method": "GET", "path": "/coupons/{cid}"},
            {"method": "POST", "path": "/orders",
             "body": {"coupon_id": "{cid}", "amount": 50}}]}], f"http://127.0.0.1:{port}")
        f = tmp_path / "smoke_gen.py"
        f.write_text(s, encoding="utf-8")
        r = subprocess.run([sys.executable, str(f)], capture_output=True,
                           text=True, timeout=60, encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert "SMOKE_JSON=" in r.stdout, r.stdout + r.stderr
        data = _json.loads(r.stdout.split("SMOKE_JSON=", 1)[1].strip().splitlines()[0])
        assert data[0]["ok"] is True
        assert received[1] == ("GET", "/coupons/1", None)              # 路径 {cid}→1
        assert received[2][2] == {"coupon_id": 1, "amount": 50}        # body {cid}→1
    finally:
        srv.shutdown()
