"""LLM 返回解析兜底回归网：鲁棒提取 JSON + 用例清洗。"""
from test_agent.skills.gen_tests import _parse_cases

_CLEAN = ('{"imports": "from calc import add", "cases": ['
          '{"name": "t1_等价类划分", "method": "等价类划分", '
          '"code": "def test_t1_等价类划分():\\n    assert add(1, 2) == 3"}]}')


def test_parse_clean_json():
    data = _parse_cases(_CLEAN)
    assert data is not None
    assert len(data["cases"]) == 1
    assert data["cases"][0]["method"] == "等价类划分"


def test_parse_json_in_code_fence():
    data = _parse_cases(f"好的，以下是测试用例：\n```json\n{_CLEAN}\n```\n请查收。")
    assert data is not None and len(data["cases"]) == 1


def test_parse_garbage_returns_none():
    assert _parse_cases("抱歉，我无法生成。") is None
    assert _parse_cases("") is None


def test_parse_cases_without_test_func_are_filtered():
    dirty = '{"cases": [{"name": "a", "code": "print(123)"}, ' \
            '{"name": "b", "code": "def test_b():\\n    assert True"}]}'
    data = _parse_cases(dirty)
    assert data is not None and [c["name"] for c in data["cases"]] == ["b"]


def test_parse_all_invalid_returns_none():
    assert _parse_cases('{"cases": [{"name": "a", "code": "print(1)"}]}') is None


def test_parse_cases_not_a_list_returns_none():
    assert _parse_cases('{"cases": "oops"}') is None


def test_parse_python_literals_tolerant():
    """DeepSeek 模型漂移实锤（2026-10-30 D8 补跑）：JSON 里输出 Python 字面量
    True/False/None——宽容解析只替换报错位置（字符串外），code 内的 True 不误伤。"""
    dirty = ('{"cases": [{"name": "t1", "code": "def test_t1():\\n    assert f() is True", '
             '"expected": True}, '
             '{"name": "t2", "code": "def test_t2():\\n    assert not g()", "expected": None}]}')
    data = _parse_cases(dirty)
    assert data is not None and len(data["cases"]) == 2
    assert "assert f() is True" in data["cases"][0]["code"]      # 字符串内 True 未被误伤
    assert data["cases"][1]["expected"] is None


def test_parse_top_level_slot_form_tolerated():
    """LLM 偶尔把整响应用 T2 槽位格式返回（无标准 cases 包装）——包一层成单条模板用例。"""
    t2 = ('{"imports": "from checker import check_length", "target": "check_length({v})", '
          '"cases": [{"value": "Abcdefg1!", "expected": true, "label": "min"}, '
          '{"value": "x", "expected": false, "label": "too short"}]}')
    data = _parse_cases(t2)
    assert data is not None and len(data["cases"]) == 1
    assert data["cases"][0]["template"] == "T2"
    assert data["cases"][0]["slots"]["target"] == "check_length({v})"


def test_parse_python_literals_in_slot_form():
    dirty = ('{"target": "check_length({v})", "cases": '
             '[{"value": 1, "expected": True, "label": "min"}]}')
    data = _parse_cases(dirty)
    assert data is not None and data["cases"][0]["template"] == "T2"


def test_parse_still_honest_on_real_garbage():
    assert _parse_cases("完全不是 JSON 的回复") is None
    assert _parse_cases('{"cases": [{ Broken }]') is None


# ---------- 降级提取（2026-11-17 卡 2，task1278 三份留档实证的两形态） ----------

def test_parse_trailing_markdown_appendix_raw_decode():
    """形态①：JSON 完整但尾部带 ```java 辅助类附录——贪婪匹配抓附录 } 报 Extra data，
    raw_decode 首对象救回全部用例（1278 留档 005545 同构）。"""
    body = _CLEAN
    text = body + "\n```java\n// 辅助类定义（需与用例一同放入）\nstatic class Shape { int x; }\n```"
    data = _parse_cases(text)
    assert data is not None and len(data["cases"]) == 1, "附录不得毁掉完整 JSON"


def test_parse_double_json_block_first_wins():
    """形态①变体（1278 留档 005738）：模型先给完整 JSON 又补一个 ```json 块——
    首对象胜出，不得因第二个块的存在整体作废。"""
    second = '{"imports": "x", "cases": [{"name": "n", "code": "whatever"}]}'
    text = f"{_CLEAN}\n```json\n{second}\n```"
    data = _parse_cases(text)
    assert data is not None and data["cases"][0]["name"] == "t1_等价类划分"


def test_parse_truncated_mid_array_salvage():
    """形态②（1278 留档 005906）：输出在 cases 数组中部截断——截尾闭合救回完整条目，
    截断处残缺条目不硬造。"""
    case = ('{"name": "t{N}_等价类划分", "method": "等价类划分", "code": '
            '"def test_t{N}():\n    assert add(1, 2) == 3"}')
    full = '{"imports": "from calc import add", "cases": [' \
           + ", ".join(case.replace("{N}", str(i)) for i in range(1, 9)) + "]}"
    truncated = '{"imports": "from calc import add", "cases": [' \
                + ", ".join(case.replace("{N}", str(i)) for i in range(1, 6)) \
                + ", " + case.replace("{N}", "6")[:40]     # 第 6 条切在 code 字符串中部
    data = _parse_cases(truncated)
    assert data is not None, "截断输出必须截尾救回"
    names = [c["name"] for c in data["cases"]]
    assert len(names) == 5, f"应救回 5 条完整用例，实得 {len(names)}"
    assert all(n.startswith("t") for n in names)


def test_parse_truncated_code_string_cut_is_safe():
    """截尾切点落在字符串字面量内部（code 含 { }）必须解析失败后回退更早边界，
    不得产出损坏条目。"""
    tricky = ('{"cases": [{"name": "a", "code": "def test_a():\n'
              '    d = {1: 2}\n    assert d[1] == 2"}, '
              '{"name": "b", "code": "def test_b():\n    assert {\'k\'} == {\'k\'}"}')
    truncated = tricky + ', {"name": "c", "code": "def test_c():\n    x = {1: '
    data = _parse_cases(truncated)
    assert data is not None
    assert [c["name"] for c in data["cases"]] == ["a", "b"], "残缺的 c 不得混入"


def test_parse_salvage_honest_on_real_garbage():
    """降级提取也不造假：无对象边界/截断到一条完整用例都没有 → 仍 None（诚实降级）。"""
    assert _parse_cases('{"cases": [ {"na') is None
    assert _parse_cases("完全不是 JSON 的回复 {") is None


# ---------- A0-3（2026-11-27）：合法 JSON 不许先变形再解析；Extra data 拖尾可救 ----------

# task 2680 原文（生产库 agent_steps.decision_json 逐字摘录，135 字符）：句尾杂散
# `}` → 标准 json.loads 报 Extra data → 旧 _loads_tolerant 同死 → unparseable×2
# → hard_error（S-K1=44/45 的唯一败因）
_T2680_RAW = ('{"action": "call", "capability": "test.observe.read", '
              '"args": {"path": ".", "mode": "list"}, "reason": '
              '"规划阶段先列出工作区结构，定位优惠券 API 的实现文件"}}')


def test_tolerant_tries_plain_loads_before_rewrite(monkeypatch):
    """A0-3：_loads_tolerant 必须先标准 json.loads，成功即直返——不许先做表达式
    替换再解析（替换把合法 JSON 的中文字符串转义撑大——uXXXX 形态，2680 实测 135→250）。
    植入一个会改坏文本的替换器：合法 JSON 路径根本不该触碰它。"""
    from test_agent.skills import gen_tests

    def _corrupt(m):
        return '"XX"'                       # 只要被调用就把片段改坏

    monkeypatch.setattr(gen_tests, "_eval_str_expr", _corrupt)
    good = '{"cases": [{"name": "t1", "code": "def test_t1():\\n    assert f(1) == 2"}]}'
    data = gen_tests._loads_tolerant(good)
    assert data["cases"][0]["name"] == "t1"


def test_tolerant_extra_data_trailing_brace_rescued():
    """A0-3：Extra data（对象后拖尾冗余）取首个完整对象救回——与 _salvage_json_output
    形态①同哲学。2680/2688 的杂散尾 `}` 两次不可解析→hard_error，不许再发生。"""
    from test_agent.skills import gen_tests
    data = gen_tests._loads_tolerant(_T2680_RAW)
    assert data["action"] == "call"
    assert data["args"] == {"path": ".", "mode": "list"}


def test_extract_json_2680_original_returns_action():
    """A0-3 端到端：tool_loop._extract_json 吃 2680 原文必须出 action=call
    （旧链两候选全走 _loads_tolerant → 同死 → unparseable×2 → hard_error）。"""
    from test_agent.skills import tool_loop
    action = tool_loop._extract_json(_T2680_RAW)
    assert action is not None
    assert action["action"] == "call"
    assert action["capability"] == "test.observe.read"


# ---------- tsc 闸编译目标（2026-11-17 卡 2，1295 ts-ky 实锤） ----------

def test_tsc_gate_modern_lib():
    """文件模式忽略项目 tsconfig（TS 语义）→ 旧 es2020 默认 lib 对现代库源码
    （Object.hasOwn=ES2022）恒 TS2550 连坐生成物。target/lib 升 es2022 后
    现代库不再被冤枉，生成代码类型错仍全抓（拦截力不变）。"""
    from test_agent.skills.gen_tests import collect_gate_cmd
    cmd = collect_gate_cmd("typescript", "test_gen_r1.test.ts")
    assert cmd[cmd.index("--target") + 1] == "es2022"
    assert cmd[cmd.index("--lib") + 1] == "es2022,dom"
    assert "--skipLibCheck" in cmd and "--noEmit" in cmd
