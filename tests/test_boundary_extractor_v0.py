# -*- coding: utf-8 -*-
"""确定性边界器 v0 单测（阶段 0'，2026-11-28）：先红后绿；含防泄漏守卫。"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.boundary_extractor_v0 import (  # noqa: E402
    extract_discount, extract_ranges, fp_suite, lib_suite, rest_suite)

LIB_REQ = (
    "校验密码长度是否在 `[min_len, max_len]` 闭区间内。返回 True；否则返回 False。\n"
    "包含至少一个大写英文字母；小写英文字母；数字；特殊字符。\n"
    "长度恰好等于 min_len 或 max_len 时应返回 True。\n"
    "空字符串返回 False。"
)
REST_REQ = (
    "创建会议室 POST /rooms：name 长度 1~20，capacity 1~20 整数。\n"
    "创建预约 POST /bookings：start_hour 8~20，end_hour 9~21；预约时长必须在 1~4 小时"
    "闭区间内（否则 400）；两个时段重叠（前场结束 == 后场开始不算重叠）应返回 409。\n"
    "计价：时长 ≥3 小时打 9 折（总价 × 0.9）。"
)
COUPON_REQ = "创建优惠券：code 长度 2~8、percent 1~100（整数），成功返回 201 与完整字段"


def test_extract_ranges_finds_all():
    assert (1, 4) in extract_ranges(REST_REQ)
    assert (8, 20) in extract_ranges(REST_REQ)
    assert (8, 64) in extract_ranges(LIB_REQ.replace("[min_len, max_len]", "[8, 64]"))
    assert (2, 8) in extract_ranges(COUPON_REQ)


def test_extract_discount_parses_zhe():
    assert extract_discount(REST_REQ) == (3, 0.9)
    assert extract_discount("无计价条款") is None


def _mk_lib_project(tmp_path):
    src = (
        "def check_length(password, min_len=8, max_len=64):\n"
        "    n = len(password)\n"
        "    if n < min_len:\n        return False\n"
        "    if n >= max_len:\n        return False\n"
        "    return True\n"
        "\n"
        "def is_strong(password, min_len=8, max_len=64):\n"
        "    return check_length(password, min_len, max_len)\n"
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "checker.py").write_text(src, encoding="utf-8")
    return tmp_path


def test_lib_suite_generates_endpoint_cases(tmp_path):
    proj = _mk_lib_project(tmp_path / "proj")
    src = lib_suite(proj, LIB_REQ)
    compile(src, "gen.py", "exec")
    tree = ast.parse(src)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    # 端点四件套 × 2 个谓词函数（check_length / is_strong）
    for f in ("check_length", "is_strong"):
        assert any(n.startswith(f"test_bnd_{f}_len_eq_max_64") for n in names), names
        assert any(n.startswith(f"test_bnd_{f}_len_below_7") for n in names)
    # 长度 64 的 base 必须满足四类字符（否则 is_strong 用例语义错）
    fnode = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and "len_eq_max_64" in n.name)
    arg = next(c.args[0].value for c in ast.walk(fnode)
               if isinstance(c, ast.Call) and c.args
               and isinstance(c.args[0], ast.Constant)
               and isinstance(c.args[0].value, str))
    pw = arg
    assert len(pw) == 64 and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_rest_suite_compiles_and_targets():
    src = rest_suite(REST_REQ)
    compile(src, "gen.py", "exec")
    tree = ast.parse(src)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert any("duration_4h" in n for n in names), names
    assert any("duration_5h" in n for n in names), "越界端点必须生成"
    assert any("adjacent_no_conflict" in n for n in names), "相邻时段靶点必须生成"
    assert any("price_discount_at_3h" in n for n in names), "计价阈值靶点必须生成"


def test_fp_suite_compiles_and_bounds():
    src = fp_suite(COUPON_REQ)
    compile(src, "gen.py", "exec")
    assert "len_2_in_range_ok" in src and "len_8_in_range_ok" in src
    assert "len_9_out_range_rejected" in src and "percent_101_out_range_rejected" in src


def test_leakage_guard_no_ground_truth():
    """防泄漏守卫：边界器源码不得引用任何 ground_truth（只准吃需求与签名）。"""
    src = (ROOT / "scripts" / "boundary_extractor_v0.py").read_text(encoding="utf-8")
    assert "ground_truth" not in src
    assert "planted_defects" not in src
