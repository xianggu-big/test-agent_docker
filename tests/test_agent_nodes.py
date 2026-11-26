"""包 1 图锁测试（2026-11-20 设计稿 §3.8 末两条）：
1) 旗标关：compile 后节点集合与现状逐字节一致（图形态不变锁）；
2) 旗标开：多 strategy/diagnose 两节点+边接通（analyze→strategy→plan、
   evaluate repair_gen→diagnose→generate）。
用 tmp 库+假 runner 构 deps——零容器、零 LLM（只 compile 不 invoke）。"""
import types

import pytest

from test_agent import config
from test_agent.graph import builder
from test_agent.store import db


def _deps(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    return types.SimpleNamespace(
        conn=conn, registry=types.SimpleNamespace(register=lambda s: None),
        runner=types.SimpleNamespace(cleanup=lambda n: None,
                                     client=types.SimpleNamespace(containers=None)),
        skills={})


def test_flag_off_graph_unchanged(tmp_path, monkeypatch):
    """关=逐字节：节点集合=既有 10 节点，无 strategy/diagnose。"""
    monkeypatch.setattr(config, "AGENT_LOOP_ENABLED", False)
    g = builder.build_graph(_deps(tmp_path, monkeypatch))
    nodes = set(g.get_graph().nodes)
    assert "strategy" not in nodes and "diagnose" not in nodes
    assert {"clone", "analyze", "plan", "provision", "generate", "run",
            "evaluate", "report", "review", "static_draft"} <= nodes


def test_flag_on_graph_has_agent_nodes(tmp_path, monkeypatch):
    """开=+strategy/+diagnose 且边接通（compile 成功即结构合法）。"""
    monkeypatch.setattr(config, "AGENT_LOOP_ENABLED", True)
    g = builder.build_graph(_deps(tmp_path, monkeypatch))
    nodes = set(g.get_graph().nodes)
    assert {"strategy", "diagnose"} <= nodes
    ge = builder.build_exec_graph(_deps(tmp_path, monkeypatch))
    assert "diagnose" in set(ge.get_graph().nodes)
    assert "strategy" not in set(ge.get_graph().nodes), "执行图不进 strategy（设计稿 §3.5）"


def test_state_has_agent_fields():
    """TaskState 九字段声明锁（未声明=LangGraph schema 过滤=366 教训）。"""
    from test_agent.graph.state import TaskState
    hints = TaskState.__annotations__
    for f in ("agent_loop_enabled", "agent_strategy", "agent_steps", "agent_budget",
              "agent_evidence", "agent_diagnosis", "agent_capability_grants",
              "agent_plugin_requests", "agent_loop_stop"):
        assert f in hints, f"TaskState 缺 {f}"


def test_phase_groups_has_agent_loop():
    """包 3：PHASE_GROUPS 显式落组（V5 坑预防——不落组=静默走 default 档）。"""
    from test_agent.skills.llm_profile import phase_group
    for p in ("agent_loop", "agent_strategy", "agent_diagnose"):
        assert phase_group(p) == "agent_loop", f"{p} 必须命中 agent_loop 组"


def test_phase_groups_has_module_profile():
    """B5（2026-11-27）：module_profile 节点 phase 必须显式落组——
    不落组=静默走 other/default 档（V5 坑同族第四起）。"""
    from test_agent.skills.llm_profile import phase_group
    assert phase_group("module_profile") == "module_profile"
    assert phase_group(" module_profile ") == "module_profile"   # strip 口径
