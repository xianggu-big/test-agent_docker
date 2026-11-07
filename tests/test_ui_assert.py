"""黑包阶段3·UI Playwright 测试矩阵（docs/黑盒能力补齐任务包-20261106.md 测试矩阵行3）。

单测锁闸门与解析器（改坏实现必须变红）：
- selector 真值闸负例（虚构元素整案剔除并留痕）与正例（三形态命中放行）
- 操作序列 schema 校验（未知动作类型/缺字段拒绝）
- 内置最小静态 HTML 快照 fixture（不依赖网络）
- 截图/trace 落盘路径校验（渲染文本含证据路径字面量）
- junitxml → 场景结果映射、落库幂等、缺陷报告单、skill 注册
"""
import json
import re

import pytest

from test_agent.skills.ui_assert import (
    build_defect_report,
    merge_ui_rows,
    parse_selector,
    render_pytest,
    scenario_gate,
    store_ui_asserts,
)

# —— 内置最小静态 HTML 快照 fixture（collect_dom_snapshot 输出同形态，不依赖网络） ——
SNAPSHOT = {
    "url": "http://localhost:8080/",
    "title": "待办清单",
    "roles": [
        {"role": "heading", "name": "待办清单"},
        {"role": "textbox", "name": "新待办"},
        {"role": "button", "name": "添加"},
        {"role": "button", "name": "删除 buy milk"},
    ],
    "ids": ["new-todo", "add-btn", "todo-list"],
    "texts": ["待办清单", "新待办", "添加", "buy milk", "删除 buy milk"],
}
# texts 含裸文本（StaticText）与按钮名两种形态——与真机快照一致（模板发现依赖成对结构）


def _scen(name, steps):
    return {"name": name, "steps": steps}


GOOD_STEPS = [
    {"action": "goto", "url": "http://localhost:8080/"},
    {"action": "fill", "selector": 'role=textbox name="新待办"', "value": "buy milk"},
    {"action": "click", "selector": 'role=button name="添加"'},
    {"action": "expect_visible", "selector": "text=待办清单"},
]


# —— selector 解析（三形态语法） ——

def test_parse_selector_role_ok():
    assert parse_selector('role=button name="添加"') == ("role", "button", "添加")


def test_parse_selector_text_ok():
    assert parse_selector("text=待办清单") == ("text", "待办清单")


def test_parse_selector_id_ok():
    assert parse_selector("#add-btn") == ("id", "add-btn")


def test_parse_selector_bad_grammar():
    # 语法外形态一律不可解析（xpath/CSS/裸类名/缺 name 的 role）
    assert parse_selector("xpath=//div[@id='x']") is None
    assert parse_selector(".add-btn") is None
    assert parse_selector('role=button') is None
    assert parse_selector("") is None
    assert parse_selector(None) is None


# —— 真值闸 + schema 闸（scenario_gate） ——

def test_gate_passes_all_three_forms():
    scens = [_scen("主流程", GOOD_STEPS + [
        {"action": "expect_text", "selector": "#new-todo", "text": "buy milk"},
        {"action": "expect_visible", "selector": 'role=button name="删除 buy milk"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert len(kept) == 1 and not rejected


def test_gate_fabricated_role_rejected_with_trace():
    scens = [_scen("虚构角色", [
        {"action": "click", "selector": 'role=tab name="设置"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and len(rejected) == 1
    assert rejected[0]["verdict"] == "rejected"
    assert 'role=tab name="设置"' in rejected[0]["rejected_reason"]
    assert rejected[0]["scenario"] == "虚构角色"


def test_gate_fabricated_name_rejected():
    scens = [_scen("虚构名字", [
        {"action": "click", "selector": 'role=button name="保存全部"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and "保存全部" in rejected[0]["rejected_reason"]


def test_gate_fabricated_id_and_text_rejected():
    scens = [
        _scen("虚构 id", [{"action": "click", "selector": "#nonexistent-btn"}]),
        _scen("虚构 text", [{"action": "expect_visible", "selector": "text=不存在的文本"}]),
    ]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and {r["scenario"] for r in rejected} == {"虚构 id", "虚构 text"}


def test_gate_unknown_action_rejected():
    scens = [_scen("未知动作", [
        {"action": "drag_and_drop", "selector": "#add-btn", "value": "x"},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and "drag_and_drop" in rejected[0]["rejected_reason"]


def test_gate_missing_fields_rejected():
    cases = {
        "goto 缺 url": [{"action": "goto"}],
        "click 缺 selector": [{"action": "click"}],
        "fill 缺 value": [{"action": "fill", "selector": "#new-todo"}],
        "expect_text 缺 text": [{"action": "expect_text", "selector": "#new-todo"}],
        "steps 空列表": [],
    }
    for label, steps in cases.items():
        kept, rejected = scenario_gate([_scen(label, steps)], SNAPSHOT)
        assert not kept, label
        assert rejected[0]["verdict"] == "rejected", label


def test_gate_bad_scenario_shape_rejected():
    kept, rejected = scenario_gate([{"steps": GOOD_STEPS}], SNAPSHOT)      # 缺 name
    assert not kept
    kept, rejected = scenario_gate("not-a-list", SNAPSHOT)                  # 顶层非列表
    assert not kept and rejected


def test_gate_whole_case_semantics_on_mixed_steps():
    # 整案语义：同一场景合法步骤+虚构步骤并存 → 整案剔除（一条都不执行）
    scens = [_scen("混合", GOOD_STEPS + [
        {"action": "click", "selector": 'role=button name="幽灵按钮"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and "幽灵按钮" in rejected[0]["rejected_reason"]


def test_gate_runtime_derived_element_allowed():
    # 运行时派生元素：fill 引入的新项，断言其存在/删除按钮——执行前快照必然没有，
    # 放行规则=text 精确等于 fill 值 / role 名=模板前缀+fill 值（模板从快照成对结构自动发现）
    scens = [_scen("添加待办", [
        {"action": "goto", "url": SNAPSHOT["url"]},
        {"action": "fill", "selector": 'role=textbox name="新待办"', "value": "买牛奶"},
        {"action": "click", "selector": 'role=button name="添加"'},
        {"action": "expect_visible", "selector": "text=买牛奶"},
        {"action": "expect_visible", "selector": 'role=button name="删除 买牛奶"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert len(kept) == 1 and not rejected


def test_gate_derived_rejects_unrelated_fabrication():
    # 派生放行不豁免真虚构：与 fill 值无关的幽灵元素仍整案剔除
    scens = [_scen("夹带虚构", [
        {"action": "fill", "selector": 'role=textbox name="新待办"', "value": "买牛奶"},
        {"action": "click", "selector": 'role=button name="添加"'},
        {"action": "expect_visible", "selector": 'role=button name="幽灵 买牛奶"'},
    ])]
    kept, rejected = scenario_gate(scens, SNAPSHOT)
    assert not kept and "幽灵" in rejected[0]["rejected_reason"]


# —— 渲染层（截图/trace 落盘路径校验） ——

def test_render_pytest_contains_evidence_and_mapping():
    import base64 as b64mod
    kept, rejected = scenario_gate([_scen("添加待办", GOOD_STEPS + [
        {"action": "expect_visible", "selector": "#add-btn"},
    ])], SNAPSHOT)
    assert len(kept) == 1 and not rejected
    text = render_pytest(kept, SNAPSHOT["url"], "/work/ui_evidence")
    assert "get_by_role" in text and "get_by_text" in text and "page.locator('#'" in text
    assert "--no-sandbox" in text                      # root 下 chromium 必需
    assert "ui_evidence" in text                       # 截图/trace 落盘目录
    assert "trace.zip" in text                         # 每场景 trace 落盘
    assert "junitxml" not in text                      # junitxml 由命令行参数给，不写死
    assert "添加待办" in text                          # 场景名保留（pytest 节点可读）
    # 步骤数据级校验：三种 selector 形态都在 payload 里（数据驱动映射）
    m = re.search(r'b64decode\("([^"]+)"\)', text)
    payload = json.loads(b64mod.b64decode(m.group(1)).decode("utf-8"))
    sels = [tuple(st["sel"]) for s in payload for st in s["steps"] if st["sel"]]
    assert ("role", "textbox", "新待办") in sels and ("id", "add-btn") in sels
    assert ("text", "待办清单") in sels


def test_render_pytest_steps_are_injection_proof():
    # 场景数据以 base64+json 载入，值里的引号/换行不破坏渲染
    nasty = _scen("引号'场景\"<x>", [
        {"action": "fill", "selector": 'role=textbox name="新待办"', "value": "a'b\"c\n<d>"},
    ])
    kept, _ = scenario_gate([nasty], SNAPSHOT)
    assert len(kept) == 1
    text = render_pytest(kept, SNAPSHOT["url"], "/work/ui_evidence")
    assert "<d>" not in text and "a'b" not in text     # 原值不进代码文本（经 base64）


# —— junitxml → ui 行合并 ——

JUNIT = """<testsuite name="ui" tests="2" failures="1" errors="0">
  <testcase classname="ui_test_r1" name="test_0_添加待办" time="1.2"/>
  <testcase classname="ui_test_r1" name="test_1_删除待办" time="2.0">
    <failure message="Expect 20s Timed out">AssertionError: expected text</failure>
  </testcase>
</testsuite>"""


def _steps_payload():
    return [{"scenario": "添加待办", "steps": [
        {"i": 0, "action": "goto", "selector": "", "verdict": "passed", "error": ""},
        {"i": 1, "action": "fill", "selector": "#new-todo", "verdict": "passed", "error": ""}]},
        {"scenario": "删除待办", "steps": [
            {"i": 0, "action": "goto", "selector": "", "verdict": "passed", "error": ""},
            {"i": 1, "action": "click", "selector": 'role=button name="删除 buy milk"',
             "verdict": "failed", "error": "TimeoutError: click 5s"}]}]


def test_merge_ui_rows_maps_junit_and_steps():
    rows = merge_ui_rows(JUNIT, _steps_payload(), trace_dir="/work/ui_evidence")
    scen_rows = [r for r in rows if r["step_no"] == 0]
    assert {r["scenario"] for r in scen_rows} == {"添加待办", "删除待办"}
    by_name = {r["scenario"]: r for r in scen_rows}
    assert by_name["添加待办"]["verdict"] == "passed"
    assert by_name["删除待办"]["verdict"] == "failed"
    assert by_name["删除待办"]["trace_path"] == "/work/ui_evidence/删除待办/trace.zip"
    step_rows = [r for r in rows if r["step_no"] > 0]
    assert len(step_rows) == 4
    failed_step = [r for r in step_rows if r["verdict"] == "failed"]
    assert failed_step[0]["action"] == "click"


def test_merge_ui_rows_junit_unparseable_honest():
    rows = merge_ui_rows("<not-xml", _steps_payload(), trace_dir="/work/ui_evidence")
    # junit 解析失败 → 场景级行如实缺席，步骤行仍保留（不伪造 verdict）
    assert not [r for r in rows if r["step_no"] == 0]
    assert len([r for r in rows if r["step_no"] > 0]) == 4


# —— 落库幂等（口径与 app_logs/db_asserts 一致） ——

def test_store_ui_asserts_idempotent(tmp_path):
    import sqlite3
    db = tmp_path / "u.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE ui_asserts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL, round_no INTEGER NOT NULL DEFAULT 1,
        scenario TEXT NOT NULL, step_no INTEGER NOT NULL DEFAULT 0,
        action TEXT NOT NULL, selector TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
        screenshot_path TEXT NOT NULL DEFAULT '', trace_path TEXT NOT NULL DEFAULT '',
        rejected_reason TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      CREATE TABLE test_tasks (id INTEGER PRIMARY KEY);
      INSERT INTO test_tasks(id) VALUES (7);
    """)
    rows = merge_ui_rows(JUNIT, _steps_payload(), trace_dir="/work/ui_evidence")
    store_ui_asserts(conn, 7, 1, rows)
    n1 = conn.execute("SELECT COUNT(*) c FROM ui_asserts WHERE task_id=7").fetchone()["c"]
    store_ui_asserts(conn, 7, 1, rows[:2])        # round≤1 重存清全任务
    n2 = conn.execute("SELECT COUNT(*) c FROM ui_asserts WHERE task_id=7").fetchone()["c"]
    assert n1 == len(rows) and n2 == 2
    store_ui_asserts(conn, 7, 2, rows[:1])        # round>1 只清同轮
    n3 = conn.execute(
        "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=7 AND round_no=2").fetchone()["c"]
    assert n3 == 1
    n_all = conn.execute("SELECT COUNT(*) c FROM ui_asserts WHERE task_id=7").fetchone()["c"]
    assert n_all == 3    # round1 的 2 行 + round2 的 1 行（round2 清理只影响同轮）


# —— 缺陷报告单 ——

def test_defect_report_includes_evidence_paths(tmp_path):
    rows = merge_ui_rows(JUNIT, _steps_payload(), trace_dir="/work/ui_evidence")
    md_path = build_defect_report("需求：支持添加/删除待办", rows,
                                  evidence_root="/work/ui_evidence",
                                  out_dir=tmp_path)
    md = (tmp_path / md_path).read_text(encoding="utf-8")
    assert "删除待办" in md and "缺陷" in md
    assert "/work/ui_evidence/删除待办/trace.zip" in md
    assert "添加待办" not in md.split("## 3")[1].split("###")[1]   # 通过场景不进缺陷清单


def test_defect_report_no_defect_honest(tmp_path):
    rows = [{"scenario": "全过场景", "step_no": 0, "action": "scene",
             "selector": "", "verdict": "passed", "error": "",
             "screenshot_path": "", "trace_path": "/t/trace.zip",
             "rejected_reason": "", "elapsed_ms": 1}]
    md_path = build_defect_report("需求", rows, evidence_root="/work",
                                  out_dir=tmp_path)
    assert md_path is None or "无疑似缺陷" in (tmp_path / md_path).read_text(encoding="utf-8")


# —— skill 注册 ——

def test_ui_assert_registered_in_builtin():
    from test_agent.skills.builtin import build_skills
    runner = type("R", (), {"client": None})()
    skills = build_skills(runner)
    assert "ui_assert" in skills
    sk = skills["ui_assert"]
    assert sk.risk_level == "medium"
    assert "scenarios" in sk.arg_schema and "snapshot" in sk.arg_schema


# —— LLM 生成层（mock make_chat，不真调） ——

def test_gen_ui_scenarios_parses_llm_json(monkeypatch):
    from test_agent.skills import ui_assert as ua
    payload = json.dumps({"scenarios": [
        {"name": "主流程", "steps": GOOD_STEPS}]}, ensure_ascii=False)
    class FakeChat:
        def __init__(self, *a, **k):
            pass
        def invoke(self, msgs):
            return type("M", (), {"content": f"```json\n{payload}\n```"})()
    monkeypatch.setattr(ua, "_make_chat", lambda **k: FakeChat())
    scens, rejected = ua.gen_ui_scenarios("k", SNAPSHOT, "需求文本", base_url="http://x/", min_scenarios=1)
    assert len(scens) == 1 and not rejected


def test_gen_ui_scenarios_unparseable_honest(monkeypatch):
    from test_agent.skills import ui_assert as ua
    class FakeChat:
        def __init__(self, *a, **k):
            pass
        def invoke(self, msgs):
            return type("M", (), {"content": "不可解析输出：没有 JSON"})()
    monkeypatch.setattr(ua, "_make_chat", lambda **k: FakeChat())
    with pytest.raises(RuntimeError):
        ua.gen_ui_scenarios("k", SNAPSHOT, "需求", base_url="http://x/", min_scenarios=1)
