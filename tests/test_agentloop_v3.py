"""任务四阶段 T 测试（AL-1..AL-5，2026-11-26 测试任务报告；先红后绿）。

AL-1 证据摘录进观察包 / AL-2 诊断失败摘要 / AL-3 菜单按阶段过滤 /
AL-4 参数提示带范围 / AL-5 提示词 v3+泛指策略机器闸 / AL-6 目标传下游 /
AL-7 targets 接线。关态回归=图锁测试+既有关态测试不动即证。
"""
import json
import types

from test_agent.skills import capabilities as caps
from test_agent.skills.capabilities import TaskCtx
from test_agent.skills.prompt_library import (AGENT_LOOP_PROMPT_VERSION,
                                              AGENT_STRATEGY_FINISH_EXAMPLE,
                                              build_agent_decide_system,
                                              _agent_strategy_section)
from test_agent.skills.registry import _validate_args


def _ctx(mode="strategy", **snap):
    from pathlib import Path
    import tempfile
    ws = Path(tempfile.mkdtemp()) / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "calc.js").write_text(
        "function clamp(x, lo, hi) { return Math.min(Math.max(x, lo), hi); }\n"
        "function add(a, b) { return a + b; }\n", encoding="utf-8")
    return TaskCtx(task_id=7, session_id=1, workspace=ws, container_name="c",
                   primary_language="javascript", image="img",
                   state_snapshot=snap, conn=None, runner=None,
                   artifacts_dir=ws.parent / "artifacts", agent_mode=mode)


# ---------- AL-4：参数提示带范围 ----------

def test_schema_hint_shows_ranges():
    ctx = _ctx()
    menu = caps.capability_menu(ctx)
    hints = {m["id"]: m["args_hint"] for m in menu}
    assert "tail_n:int(1-50)" in hints["test.observe.logs"], hints["test.observe.logs"]
    assert "limit:int(1-200)" in hints["test.observe.read"], hints["test.observe.read"]
    assert "feedback*:string(≤1500)" in hints["test.gen.cases"], hints["test.gen.cases"]
    # 容器类能力（test.run.unit）已被 strategy 菜单过滤——范围提示在宿主能力上验证
    all_schemas = {c.id: c.inputs_schema for c in caps._build_capabilities(ctx)}
    assert "timeout_s:int(10-600)" in caps._schema_hint(all_schemas["test.run.unit"])


# ---------- AL-3：菜单按阶段过滤 ----------

def test_strategy_menu_excludes_container_caps():
    """strategy 阶段无容器——execute/需要容器的能力不进菜单。"""
    ctx = _ctx("strategy")
    ids = [m["id"] for m in caps.capability_menu(ctx)]
    assert "test.run.unit" not in ids, ids
    assert "test.observe.read" in ids
    assert "test.observe.env" in ids      # stack 探测宿主侧可用


def test_diagnose_menu_keeps_readonly():
    ctx = _ctx("diagnose")
    ids = [m["id"] for m in caps.capability_menu(ctx)]
    assert "test.observe.read" in ids and "test.observe.logs" in ids
    assert "test.edit.scaffold" not in ids


def test_phase_denial_reason_mentions_stage():
    """策略模式点名容器类能力时拒绝原因写"当前阶段不可用（容器未创建）"。"""
    ctx = _ctx("strategy")
    action = {"action": "call", "capability": "test.run.unit",
              "args": {"targets": ["src/calc.js"]}, "reason": "r"}
    from test_agent.skills import tool_policy
    menu = caps.capability_menu(ctx)
    d = tool_policy.authorize(action, ctx, menu=menu, seen_fps={},
                              budget_used={"steps": 1, "max_steps": 8,
                                           "tokens": 0, "max_tokens": 30000,
                                           "seconds": 0, "max_seconds": 600})
    assert not d.allowed
    assert "当前阶段不可用" in (d.reason or ""), d.reason


# ---------- AL-1：证据摘录进观察包 ----------

def test_read_excerpts_real_code_into_observation(tmp_path):
    """读文件后观察包含真实代码行（F-AL1 核心——模型看得见自己取证的内容）。"""
    from test_agent.skills.tool_loop import build_observation
    ctx = _ctx()
    cap = {c.id: c for c in caps._build_capabilities(ctx)}["test.observe.read"]
    from test_agent.skills.tool_loop import _execute_capability
    result, _ = _execute_capability(types.SimpleNamespace(registry=None), ctx, cap,
                                    {"path": "src/calc.js", "mode": "read"})
    assert result.status == "ok"
    assert getattr(result, "excerpt", None), "CapResult 必须带 excerpt"
    assert "clamp" in result.excerpt
    ev = [{"step": 1, "capability": "test.observe.read", "status": result.status,
           "summary": result.summary, "excerpt": result.excerpt}]   # 生产形态（run_tool_loop 同构）
    obs = build_observation(ctx, "目标", "strategy", [], ev,
                            {"max_steps": 8, "max_tokens": 30000, "max_seconds": 600},
                            {"steps": 1})
    assert "clamp" in obs, "观察包必须含证据正文摘录"


def test_observation_never_trims_output_section():
    """观察包超上限时只裁证据正文中段，"## 输出"之后的指令段永远完整。"""
    from test_agent.skills.tool_loop import build_observation
    ctx = _ctx()
    huge = [{"step": i, "capability": "test.observe.read", "status": "ok",
             "summary": "x" * 50, "excerpt": "y" * 20000} for i in range(5)]
    obs = build_observation(ctx, "目标", "strategy", [], huge,
                            {"max_steps": 8, "max_tokens": 30000, "max_seconds": 600},
                            {"steps": 5})
    assert len(obs) <= 12000 + 200      # 允许模板余量
    assert "只输出一个 JSON 对象" in obs, "指令段必须完整保留"
    assert obs.rstrip().endswith("。") or "finish" in obs[-400:]


# ---------- AL-2：诊断失败摘要（evidence #0） ----------

def test_diagnose_zero_exec_summary_in_observation():
    """0/0 状态下，诊断观察包含"零执行"行（模型不必再花步数取证）。"""
    from test_agent.skills.tool_loop import build_observation
    from test_agent.graph.agent_nodes import build_failure_digest
    ctx = _ctx("diagnose")
    snap = {"round_no": 1, "last_exit": 0, "last_stdout": "",
            "last_stderr": "", "case_counts": {"passed": 0, "failed": 0, "total": 0}}
    digest = build_failure_digest(snap)
    assert digest and "零执行" in digest
    obs = build_observation(ctx, "诊断上一轮失败", "diagnose", [], [],
                            {"max_steps": 3, "max_tokens": 10000, "max_seconds": 600},
                            {"steps": 0}, pre_evidence=[digest])
    assert "零执行" in obs


def test_diagnose_digest_lists_failure_and_signals():
    """含 Cannot find module 的输出：失败清单有该用例、可疑信号有它。"""
    from test_agent.graph.agent_nodes import build_failure_digest
    snap = {"round_no": 2, "last_exit": 1,
            "last_stdout": "",
            "last_stderr": " FAIL  src/calc.test.js\n  ● Cannot find module './calc_nope'",
            "case_counts": {"passed": 0, "failed": 12, "total": 12},
            "failure_lines": ["calc.test.js > clamp 基准 | Cannot find module './calc_nope'"]}
    digest = build_failure_digest(snap)
    assert "calc.test.js" in digest and "Cannot find module" in digest
    assert "可疑信号" in digest      # 特征表命中（attribution 同源）


# ---------- AL-5：提示词 v3 + 泛指机器闸 ----------

def test_prompt_version_v3():
    assert AGENT_LOOP_PROMPT_VERSION == "v3"


def test_v3_example_names_real_symbols_and_passes_gates():
    """v3 示例=模型真正看得到的形态（点名具体符号），且仍过 Q4 两闸（示例⊆菜单、参数⊆schema）。"""
    ctx = _ctx()
    schemas = {c.id: c.inputs_schema for c in caps._build_capabilities(ctx)}
    idx = AGENT_STRATEGY_FINISH_EXAMPLE.index('{"action": "finish"')
    pri = json.loads(AGENT_STRATEGY_FINISH_EXAMPLE[idx:])["strategy"]["priorities"]
    for p in pri:
        assert p["capability_id"] in schemas
        assert _validate_args(schemas[p["capability_id"]], p.get("args_hint") or {}) is None
    assert "clamp" in AGENT_STRATEGY_FINISH_EXAMPLE      # 点名具体符号


def test_v3_forbids_generic_targets_in_prompt():
    assert "禁止" in build_agent_decide_system("strategy")
    assert "./..." in build_agent_decide_system("strategy")


def test_diagnose_prompt_has_finish_example_and_discipline():
    s = build_agent_decide_system("diagnose")
    assert "diagnosis" in s and "证据引用" in s
    assert "失败摘要已经够用" in s       # 新纪律：摘要够用直接收尾


def test_generic_strategy_gate_retry_then_flag():
    """泛指策略：第一次 finish 不算数→追加重试证据；再次泛指→接受但 strategy_valid=False。"""
    from test_agent.skills.tool_loop import _strategy_is_generic
    generic = {"priorities": [{"order": 1, "capability_id": "test.gen.cases",
                               "why": "补齐测试", "args_hint": {"feedback": "补齐测试"}}]}
    assert _strategy_is_generic(generic) is True
    concrete = {"priorities": [{"order": 1, "capability_id": "test.gen.cases",
                                "why": "clamp 第 9 行 x==hi 应返回 hi，补边界用例",
                                "args_hint": {"feedback": "clamp x==hi 边界"}}]}
    assert _strategy_is_generic(concrete) is False


# ---------- AL-6：目标传下游 ----------

def test_strategy_section_carries_targets():
    strat = {"priorities": [{"order": 1, "capability_id": "test.gen.cases",
                             "why": "w", "args_hint": {"feedback": "f",
                                                       "targets": ["src/calc.js"]}}],
             "fallback": ""}
    sec = _agent_strategy_section(strat)
    assert "src/calc.js" in sec


def test_strategy_section_empty_when_absent():
    assert _agent_strategy_section(None) == ""


# ---------- AL-7：targets 接线进生成提示词 ----------

def test_gen_targets_section_injected():
    from test_agent.skills.prompt_library import _gen_targets_section
    assert "src/calc.js" in _gen_targets_section(["src/calc.js"])
    assert _gen_targets_section(None) == ""
    assert _gen_targets_section([]) == ""


# ---------- 关态回归锚（既有图锁测试不动即证；此处补一条快速哨兵） ----------

def test_disabled_loop_prompt_untouched():
    """关态哨兵：循环关闭时 strategy 段注入为空串（plan 提示词零变化）。"""
    assert _agent_strategy_section({}) == ""
