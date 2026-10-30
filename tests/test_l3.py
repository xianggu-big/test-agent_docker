"""L3 系统测试半自动（D5）机制单测 —— fake-LLM，不依赖 Docker/网络。"""
import json

import pytest

from test_agent import config
from test_agent.skills.scenario_gen import (apply_smoke_results, confirm_scenario,
                                            gen_scenarios_llm, l3_metrics,
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
        assert counts == {"auto_pass": 1, "pending_confirm": 1}
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
