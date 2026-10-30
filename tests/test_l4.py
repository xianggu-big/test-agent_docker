"""L4 验收辅助（GWT 生成+确认率）与 D6 转化器机制单测 —— fake-LLM/无容器。"""
import json

import pytest

from test_agent.skills.scenario_gen import (l4_metrics, parse_acceptance,
                                            render_acceptance_module)

ENDPOINTS = [{"method": "POST", "path": "/coupons"},
             {"method": "POST", "path": "/orders"}]

GOOD = json.dumps({"scenarios": [
    {"title": "创建优惠券验收", "requirement_ref": "验收条件1",
     "gwt": {"given": "系统空闲", "when": "POST /coupons code=X",
             "then": "返回 201 与完整字段"},
     "steps": [{"method": "POST", "path": "/coupons", "body": {"code": "X"},
                "expect_status": 201}]}]})


class TestParseAcceptance:
    def test_gwt结构保留_闸同源(self):
        valid, rej = parse_acceptance(GOOD, ENDPOINTS)
        assert len(valid) == 1 and valid[0]["gwt"]["then"].startswith("返回 201")
        assert rej == []

    def test_虚构接口剔除(self):
        bad = json.dumps({"scenarios": [{"title": "t", "steps": [
            {"method": "GET", "path": "/ghost"}]}]})
        valid, rej = parse_acceptance(bad, ENDPOINTS)
        assert valid == [] and "不在端点清单" in rej[0]


class TestRender:
    def test_渲染可编译且含引用链(self):
        scenarios = [{"title": "创建并下单", "expected": "201",
                      "steps": [{"method": "POST", "path": "/coupons", "body": {"code": "X"},
                                 "expect_status": 201},
                                {"method": "POST", "path": "/orders",
                                 "body": {"coupon_id": "{cid}", "amount": 100},
                                 "expect_status": 201}]}]
        src, cases = render_acceptance_module(scenarios, "http://127.0.0.1:9000")
        compile(src, "acceptance", "exec")
        assert "sub_body(" in src and "sub_path(" in src
        assert cases[0]["name"] == "test_acc_创建并下单"

    def test_expect_status落断言(self):
        src, _ = render_acceptance_module([{"title": "t", "steps": [
            {"method": "GET", "path": "/coupons", "expect_status": 200}]}], "http://x")
        assert "assert _r.status_code == 200" in src


class TestL4Metrics:
    def _conn(self, rows):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE scenario_cases (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "task_id INTEGER, level TEXT, title TEXT, status TEXT)")
        conn.execute("CREATE TABLE test_scripts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "task_id INTEGER, framework TEXT)")
        for st in rows:
            conn.execute("INSERT INTO scenario_cases(task_id, level, title, status) "
                         "VALUES (1, 'L4', 't', ?)", (st,))
        return conn

    def test_确认率与转化计数(self):
        conn = self._conn(["confirmed_pass", "confirmed_scenario", "pending"])
        sid = conn.execute("INSERT INTO test_scripts(task_id, framework) VALUES (1, 'acceptance')").lastrowid
        conn.commit()
        m = l4_metrics(conn, 1)
        assert m["total"] == 3 and m["confirmed"] == 2 and m["confirm_rate"] == 0.667
        assert m["converted"] == 1

    def test_无行整体缺席(self):
        assert l4_metrics(self._conn([]), 1) is None
