"""AI 生成用例（第 3 轮 · 五种方法知识库）"""
import pytest
from wrap import wrap

def test_wrap_等价类划分_多词超宽():
    result = wrap('hello world foo', 7)
    assert isinstance(result, list)
    assert result == ['hello', 'world', 'foo']

def test_wrap_等价类划分_单词顺序保持():
    text = 'the quick brown fox jumps'
    result = wrap(text, 10)
    assert ' '.join(result) == text

def test_wrap_边界值分析_每行长度不超过cols():
    result = wrap('aaa bbb ccc ddd', 7)
    assert all(len(line) <= 7 for line in result)

def test_wrap_边界值分析_单词长度等于cols():
    result = wrap('abcdefg hi', 7)
    assert result[0] == 'abcdefg'
    assert len(result[0]) == 7

def test_wrap_边界值分析_文本长度等于cols():
    result = wrap('abcdefg', 7)
    assert result == ['abcdefg']

def test_wrap_边界值分析_文本长度小于cols():
    result = wrap('abc', 10)
    assert result == ['abc']

def test_wrap_边界值分析_cols为1():
    result = wrap('a b c', 1)
    assert result == ['a', 'b', 'c']
    assert all(len(line) <= 1 for line in result)

def test_wrap_错误推测法_超长单词不拆分():
    result = wrap('abcdefghij x', 5)
    assert result == ['abcdefghij', 'x']

def test_wrap_错误推测法_空字符串():
    result = wrap('', 5)
    assert result == []

def test_wrap_错误推测法_连续空格():
    result = wrap('aa  bb  cc', 4)
    assert result == ['aa', 'bb', 'cc']

def test_wrap_判定表_断行处空格不保留():
    result = wrap('hello world', 5)
    assert result == ['hello', 'world']
    assert all(not line.startswith(' ') and not line.endswith(' ') for line in result)

def test_wrap_语句分支覆盖_无空格可断行():
    result = wrap('abcdefgh', 3)
    assert result == ['abc', 'def', 'gh']
