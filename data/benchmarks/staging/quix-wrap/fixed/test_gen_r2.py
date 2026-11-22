"""AI 生成用例（第 2 轮 · 五种方法知识库）"""
import pytest
from wrap import wrap

def test_wrap_长文本含空格_等价类划分():
    result = wrap('hello world foo', 7)
    assert isinstance(result, list)
    assert result == ['hello', 'world', 'foo']

def test_wrap_断行空格不保留_等价类划分():
    result = wrap('aa bb cc', 5)
    assert result == ['aa bb', 'cc']
    for line in result:
        assert not line.startswith(' ')
        assert not line.endswith(' ')

def test_wrap_每行长度不超过cols_边界值分析():
    result = wrap('the quick brown fox jumps', 10)
    assert all(len(line) <= 10 for line in result)

def test_wrap_单词长度等于cols_边界值分析():
    result = wrap('abcde fghij', 5)
    assert result == ['abcde', 'fghij']
    assert all(len(line) == 5 for line in result)

def test_wrap_超长单词不拆分_错误推测法():
    # 假设：长度大于 cols 的单词不被拆分，单独成行（行长度可能超过 cols）
    result = wrap('abcdefghij', 3)
    assert result == ['abcdefghij']

def test_wrap_保持单词顺序_等价类划分():
    text = 'one two three four'
    result = wrap(text, 9)
    assert ' '.join(result) == text

def test_wrap_cols为正整数_边界值分析():
    result = wrap('a b c', 1)
    assert result == ['a', 'b', 'c']

def test_wrap_文本长度等于cols_边界值分析():
    result = wrap('abcde', 5)
    assert result == ['abcde']

def test_wrap_文本长度小于cols_边界值分析():
    result = wrap('abc', 5)
    assert result == ['abc']

def test_wrap_空字符串_错误推测法():
    # 假设：空字符串返回空列表
    result = wrap('', 5)
    assert result == []

def test_wrap_连续多个空格_错误推测法():
    # 假设：连续空格视为分隔符，不产生空行元素
    result = wrap('aa  bb', 4)
    assert result == ['aa', 'bb']
    assert all(line != '' for line in result)

def test_wrap_多条件组合_判定表():
    # 条件1: 长度>cols 且含空格 -> 按空格断行
    assert wrap('hello world', 5) == ['hello', 'world']
    # 条件2: 长度<=cols -> 单元素原文本
    assert wrap('hello', 5) == ['hello']
    # 条件3: 长度>cols 且无空格 -> 单词不拆分单独成行
    assert wrap('abcdefgh', 3) == ['abcdefgh']
    # 条件4: 长度<=cols 且无空格 -> 单元素原文本
    assert wrap('ab', 5) == ['ab']

def test_wrap_语句分支覆盖_语句分支覆盖():
    # while 真分支 + rfind 非 -1 分支
    assert wrap('hello world', 5) == ['hello', 'world']
    # while 真分支 + rfind 返回 -1 分支
    assert wrap('abcdefgh', 3) == ['abcdefgh']
    # while 假分支（文本长度 <= cols）
    assert wrap('hi', 5) == ['hi']
