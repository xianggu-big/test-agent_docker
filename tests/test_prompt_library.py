"""提示词知识库回归：五种方法一个不能少；plan 提示词（RTM）可用。"""
from test_agent.skills.prompt_library import METHODS, build_plan_prompt, build_user_prompt


def test_five_methods_present():
    names = [m["name"] for m in METHODS]
    assert names == ["等价类划分", "边界值分析", "判定表", "语句分支覆盖", "错误推测法"]


def test_user_prompt_contains_sources_requirement_and_methods():
    prompt = build_user_prompt([("calc.py", "def add(a, b):\n    return a + b\n")],
                               "测试加法与除法")
    assert "calc.py" in prompt
    assert "def add" in prompt
    assert "测试加法与除法" in prompt
    for m in METHODS:
        assert m["name"] in prompt
    assert '"imports"' in prompt and '"cases"' in prompt  # 输出格式契约在提示词里


def test_user_prompt_without_requirement_has_placeholder():
    prompt = build_user_prompt([("a.py", "x = 1")], "")
    assert "按源码行为设计" in prompt


def test_plan_prompt_mentions_rtm_and_requirement():
    prompt = build_plan_prompt("登录功能的正常与异常流程")
    assert "登录功能的正常与异常流程" in prompt
    assert "RTM" in prompt or "测试条件" in prompt
