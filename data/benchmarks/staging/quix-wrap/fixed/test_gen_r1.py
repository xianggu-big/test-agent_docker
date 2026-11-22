"""AI 生成用例（第 1 轮 · 五种方法知识库）"""
import pytest
from wrap import wrap

def test_wrap_长文本按空格切分_等价类划分():
    result = wrap('hello world foo', 7)
    assert isinstance(result, list)
    assert result == ['hello', 'world', 'foo']

def test_wrap_断行处空格不保留_等价类划分():
    result = wrap('aaa bbb ccc', 4)
    assert result == ['aaa', 'bbb', 'ccc']
    for line in result:
        assert not line.startswith(' ')
        assert not line.endswith(' ')

import pytest
from wrap import wrap


@pytest.mark.parametrize("value,expected", [
    (5, 5),
    (6, 6),
    (10, 10),
    (20, 20),
], ids=['cols=5', 'cols=6', 'cols=10', 'cols=20'])
def test_wrap_每行长度不超过cols_边界值分析(value, expected):
    assert max(len(l) for l in wrap('the quick brown fox jumps', value)) == expected

def test_wrap_单词长度等于cols_边界值分析():
    result = wrap('abcde fg', 5)
    assert result == ['abcde', 'fg']
    assert len(result[0]) == 5

def test_wrap_超长单词不拆分_错误推测法():
    # 假设：长度大于 cols 的单词不被拆分，单独成行（行长度可能超过 cols）
    result = wrap('abcdefgh ij', 5)
    assert result == ['abcdefgh', 'ij']
    assert len(result[0]) > 5

def test_wrap_保持单词顺序_等价类划分():
    text = 'one two three four five'
    result = wrap(text, 9)
    assert ' '.join(result) == text

import pytest
from wrap import wrap


@pytest.mark.parametrize("value,expected", [
    (1, True),
    (2, True),
    (3, True),
    (4, True),
    (5, True),
], ids=['cols=1', 'cols=2', 'cols=3', 'cols=4', 'cols=5'])
def test_wrap_cols正整数正常返回_边界值分析(value, expected):
    assert isinstance(wrap('ab cd', value), list) == expected

def test_wrap_空字符串_错误推测法():
    # 假设：空字符串返回空列表
    result = wrap('', 5)
    assert result == []

def test_wrap_连续空格_错误推测法():
    # 假设：连续空格视为分隔符，不产生空行元素，原始空格数量保留
    result = wrap('aa  bb', 3)
    assert '' not in result
    assert ' '.join(result) == 'aa  bb'

def test_wrap_循环与分支覆盖_语句分支覆盖():
    # 循环不执行分支：len(text) <= cols
    assert wrap('abc', 5) == []
    # end != -1 分支：在空格处断行
    assert wrap('ab cd', 3) == ['ab', 'cd']
    # end == -1 分支：无空格时强制按 cols 断行
    assert wrap('abcdef', 3) == ['abc', 'def']

def test_wrap_多条件组合_判定表():
    # 条件1: len<=cols -> 循环不执行
    assert wrap('ab cd', 10) == []
    assert wrap('abc', 3) == []
    # 条件2: len>cols 且断点前有空格 -> 空格处断行
    assert wrap('ab cd', 3) == ['ab', 'cd']
    # 条件3: len>cols 且断点前无空格 -> 强制按 cols 断行
    assert wrap('abcdef', 3) == ['abc', 'def']
