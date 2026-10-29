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
