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
