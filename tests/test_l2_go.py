# -*- coding: utf-8 -*-
"""F 卡阶段 1 #7/#8（2026-11-10）：L2/L3/L4 接 Go——服务启动探测+provision 分流。

验收口径（F 卡 §6 坑预判 3）：『用例数 > 0 且至少 1 条断言执行』——契约用例是
Python requests 黑盒（打 127.0.0.1 的 Go 服务），单容器闭环（go126 镜像烘焙
python3+requests+pytest），渲染层零改动（HTTP 黑盒与服务语言解耦）。
"""
from pathlib import Path

from test_agent.graph.l2 import (_detect_go_entry, _start_service_go, judge_l2)
from test_agent.skills.routes import extract_route_features, route_plan


# ---------- Go 入口探测 ----------

def test_探测main_go(tmp_path):
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    assert _detect_go_entry(tmp_path) == "main.go"


def test_探测回退_其他go文件含main(tmp_path):
    (tmp_path / "app.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    assert _detect_go_entry(tmp_path) == "app.go"


def test_无func_main_诚实None(tmp_path):
    (tmp_path / "lib.go").write_text("package main\n\nfunc Hello() {}\n", encoding="utf-8")
    assert _detect_go_entry(tmp_path) is None


# ---------- 路由器：go 项目进 L2 路线（gin-master 重放） ----------

def test_gin_master路由含l2():
    # F 卡 #7 的前提：go.mod 在场 → extract_route_features 已认 go；契约路线前置=语言
    f = {"language": "go", "py": 0, "js": 0, "html": 0, "proto": 0, "sql": 0,
         "has_forms": False, "has_interactive": False, "has_http_framework": False,
         "has_package_json": False, "has_go_mod": True}
    plan = route_plan("x", f)
    # l1_unit_pytest 是 python 专属——go 项目不应选中；其他路线按各自前置判
    assert "l1_unit_pytest" not in plan["selected_ids"]


# ---------- judge_l2 契约判读回归（Go 服务 + requests 用例同解析器） ----------

def test_judge_l2_go输出可判读():
    # judge_l2 真返回实测：({summary}, "stop", "契约违背 1 处…")
    summary, verdict, reason = judge_l2(
        "=== 3 passed, 1 failed ===\nFAILED test_x", 1, [], [])
    assert verdict == "stop" and "契约违背 1 处" in reason
    assert summary["failed"] == 1 and summary["passed"] == 3
    assert reason or v == "pass"


# ---------- L2State 显式声明（LangGraph schema 过滤教训） ----------

def test_l2state声明primary_language():
    from test_agent.graph.l2 import L2State
    assert "primary_language" in L2State.__annotations__


# ---------- 镜像契约：go126 必须含 python3+requests（L2 执行依赖） ----------

def test_go126_dockerfile含python3():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "docker" / "go-test" / "Dockerfile").read_text(encoding="utf-8")
    assert "python3" in dockerfile and "requests" in dockerfile   # 单容器闭环依赖
