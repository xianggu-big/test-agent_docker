"""AI 生成用例（第 1 轮 · 五种方法知识库）"""
import pytest
from tqdm._tqdm import format_meter, format_interval, format_sizeof

def test_format_meter_速率SI格式化_等价类划分():
    out = format_meter(100, 1000, 10, unit_scale=True)
    assert '10.0it/s' in out
    assert '100/1.00K' in out

def test_format_meter_速率数量级SI前缀_等价类划分():
    out = format_meter(5000, 10000, 1, unit_scale=True)
    assert '5.0Kit/s' in out

def test_format_meter_剩余时间SI格式化_等价类划分():
    out = format_meter(50, 100, 10)
    assert '[00:10<00:10' in out

def test_format_meter_剩余时间未知占位符_边界值分析():
    out1 = format_meter(0, None, 0)
    assert '?' in out1
    out2 = format_meter(0, 100, 5)
    assert '[00:05<?' in out2

def test_format_meter_elapsed为零_边界值分析():
    out = format_meter(0, 100, 0)
    assert '?it/s' in out

def test_format_meter_elapsed极小值_边界值分析():
    out = format_meter(1, 100, 1e-9)
    assert isinstance(out, str)
    assert 'it/s' in out

def test_format_meter_完成时剩余时间为零_边界值分析():
    out = format_meter(100, 100, 10)
    assert '100%|' in out
    assert '[00:10<00:00' in out

def test_format_meter_分支覆盖_total有无_语句分支覆盖():
    out_with = format_meter(10, 100, 1)
    assert '|' in out_with
    assert '%' in out_with
    out_none = format_meter(10, None, 1)
    assert '|' not in out_none
    assert out_none.startswith('10it [00:01, ')

def test_format_meter_分支覆盖_elapsed零与非零_语句分支覆盖():
    out0 = format_meter(10, 100, 0)
    assert '?it/s' in out0
    out2 = format_meter(10, 100, 2)
    assert '5.00it/s' in out2

def test_format_meter_分支覆盖_ascii与ncols_语句分支覆盖():
    out0 = format_meter(50, 100, 10, ncols=0)
    assert '#' not in out0
    out_ascii = format_meter(50, 100, 10, ncols=20, ascii=True)
    assert '#' in out_ascii
    out_uni = format_meter(50, 100, 10, ncols=20, ascii=False)
    assert '\u2588' in out_uni

def test_format_meter_判定表_total与n与elapsed组合():
    out = format_meter(50, 100, 10)
    assert '%' in out and '<' in out
    out = format_meter(0, 100, 10)
    assert '[00:10<?' in out
    out = format_meter(50, 100, 0)
    assert '?it/s' in out
    out = format_meter(50, None, 10)
    assert '|' not in out and '50it' in out

def test_format_meter_错误推测_除零与None与负数():
    assert '?it/s' in format_meter(10, 100, 0)
    assert '|' not in format_meter(10, None, 1)
    out_over = format_meter(200, 100, 10)
    assert '|' not in out_over
    out_neg = format_meter(-5, 100, 10)
    assert isinstance(out_neg, str)

def test_format_meter_错误推测_极大值与unit_scale():
    out = format_meter(10**9, 10**9, 1, unit_scale=True)
    assert '1.00G' in out
    assert '1.00Git/s' in out
