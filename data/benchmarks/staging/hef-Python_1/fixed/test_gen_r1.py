"""AI 生成用例（第 1 轮 · 五种方法知识库）"""
import pytest
from mod_separate_paren_groups import separate_paren_groups

def test_separate_paren_groups_等价类划分_多组嵌套():
    assert separate_paren_groups('( ) (( )) (( )( ))') == ['()', '(())', '(()())']

def test_separate_paren_groups_等价类划分_单组():
    assert separate_paren_groups('(())') == ['(())']

def test_separate_paren_groups_等价类划分_无空格多组():
    assert separate_paren_groups('()()()') == ['()', '()', '()']

import pytest
from mod_separate_paren_groups import separate_paren_groups


@pytest.mark.parametrize("value,expected", [
    ("''", []),
    ("'   '", []),
    ("'()'", ['()']),
    ("'(())'", ['(())']),
    ("'()()'", ['()', '()']),
    ("'(())(())'", ['(())', '(())']),
], ids=['空串', '仅空格', '单组', '单组嵌套', '两组', '两组嵌套'])
def test_separate_paren_groups_边界值分析_空串与单组(value, expected):
    assert separate_paren_groups(value) == expected

def test_separate_paren_groups_判定表_空格与嵌套组合():
    assert separate_paren_groups('( )') == ['()']
    assert separate_paren_groups('(( ))') == ['(())']
    assert separate_paren_groups('( )( )') == ['()', '()']
    assert separate_paren_groups('(( ))(( ))') == ['(())', '(())']
    assert separate_paren_groups('( ( ) )') == ['(())']
    assert separate_paren_groups('( )( ( ) )') == ['()', '(())']

def test_separate_paren_groups_语句分支覆盖_深度增减():
    assert separate_paren_groups('(()())') == ['(()())']
    assert separate_paren_groups('()(())') == ['()', '(())']
    for group in separate_paren_groups('(()())'):
        assert group.count('(') == group.count(')')
        depth = 0
        for ch in group:
            depth += 1 if ch == '(' else -1
            assert depth >= 0
        assert depth == 0

def test_separate_paren_groups_错误推测法_None与空串():
    with pytest.raises(TypeError):
        separate_paren_groups(None)
    assert separate_paren_groups('') == []
    assert separate_paren_groups('   ') == []

def test_separate_paren_groups_错误推测法_重复调用稳定():
    first = separate_paren_groups('( ) (( ))')
    second = separate_paren_groups('( ) (( ))')
    assert first == ['()', '(())']
    assert second == ['()', '(())']
    assert first == second
