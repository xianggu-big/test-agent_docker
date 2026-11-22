"""AI 生成用例（第 1 轮 · 五种方法知识库）"""
import math
import pytest
from mod_truncate_number import truncate_number

import pytest
import math
from mod_truncate_number import truncate_number


@pytest.mark.parametrize("value,expected", [
    (3.5, 0.5),
    (10.75, 0.75),
    (0.1, 0.1),
    (3.0, 0.0),
], ids=['需求示例', '大于1且小数非零', '小于1的正数', '整数形式浮点'])
def test_truncate_number_等价类划分(value, expected):
    assert truncate_number(value) == expected

import pytest
import math
from mod_truncate_number import truncate_number


@pytest.mark.parametrize("value,expected", [
    (5e-324, 5e-324),
    (0.5, 0.5),
    (1.0, 0.0),
    (1.0000000000000002, 2.220446049250313e-16),
    (1e+308, 0.0),
], ids=['最小正浮点数', '小于1代表', '整数边界1', '1+eps', '极大值'])
def test_truncate_number_边界值分析(value, expected):
    assert truncate_number(value) == expected

def test_truncate_number_错误推测_负数():
    r = truncate_number(-3.5)
    assert not (0 <= r < 1)

def test_truncate_number_错误推测_零():
    r = truncate_number(0.0)
    assert not (0 <= r < 1)

def test_truncate_number_判定表_正数区间():
    assert truncate_number(0.25) == 0.25
    assert truncate_number(7.125) == 0.125
    assert truncate_number(9.0) == 0.0

def test_truncate_number_语句分支覆盖():
    for x in (3.5, 3.0):
        r = truncate_number(x)
        assert 0 <= r < 1
