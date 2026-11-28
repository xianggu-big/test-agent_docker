# -*- coding: utf-8 -*-
"""确定性边界器 v0（阶段 0' 证伪实验，2026-11-28；提案 §13）。

命题：数值约束的"靶点命中"可以机制化——从需求文本与函数签名解析区间，确定性生成
端点/越界用例，把"区间端点命中"从"模型碰巧"（阶段 0 B 臂 1/5 格）变成每格必踩。

通用规则（只吃 REQUIREMENT.md 与签名，**禁止读取真值文件**——泄漏守卫测试锁定）：
  ①签名数值默认参 → 谓词函数（check_/is_/has_ 前缀或 bool 注解）端点用例；
  ②需求文本区间（X~Y / [X, Y] / X 到 Y）→ 端点与越界断言；
  ③计价条款（≥X 小时打 Y 折）→ 阈值断言。
项目执行模板（lib=直接调函数；REST=TestClient）是实验脚手架，主张只落在
"区间→端点用例"这一步是确定性的。

用法：
  python -I scripts/boundary_extractor_v0.py lib  --project D --requirement R --out S
  python -I scripts/boundary_extractor_v0.py rest --project D --requirement R --out S
  python -I scripts/boundary_extractor_v0.py fp   --project D --requirement R --out S
"""
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

RE_RANGE_BRACKET = re.compile(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]")
RE_RANGE_TILDE = re.compile(r"(\d+)\s*[~～]\s*(\d+)")
RE_DISCOUNT = re.compile(r"≥\s*(\d+)\s*小时打\s*(\d)\s*折.*?×\s*(0\.\d+)")
RE_PCT_RANGE = re.compile(r"percent\s+(\d+)\s*[~～]\s*(\d+)", re.I)
RE_CODE_RANGE = re.compile(r"code\s+长度\s+(\d+)\s*[~～]\s*(\d+)")


def extract_ranges(text: str) -> list[tuple[int, int]]:
    """需求文本里的数值区间（去重、保序）。"""
    out: list[tuple[int, int]] = []
    for pat in (RE_RANGE_BRACKET, RE_RANGE_TILDE):
        for m in pat.finditer(text):
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 <= lo <= hi <= 100000 and (lo, hi) not in out:
                out.append((lo, hi))
    return out


def extract_discount(text: str) -> tuple[int, float] | None:
    """计价阈值：(时长小时数, 折扣系数)。找不到返回 None。"""
    m = RE_DISCOUNT.search(text)
    if m:
        return int(m.group(1)), float(m.group(3))
    return None


def _predicates(tree) -> list[ast.FunctionDef]:
    """谓词函数：check_/is_/has_ 前缀，或 bool 返回注解。"""
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith(("check_", "is_", "has_")):
            out.append(node)
            continue
        ret = node.returns
        if ret is not None and "bool" in ast.unparse(ret):
            out.append(node)
    return out


def _numeric_default_pairs(node: ast.FunctionDef) -> list[tuple[str, int, int]]:
    """数值默认参两两成对（min/max 语义）：按出现顺序取前两个数值默认。"""
    nums: list[tuple[str, int]] = []
    for d in node.args.defaults:
        try:
            v = ast.literal_eval(d)
        except (ValueError, SyntaxError):
            continue
        if isinstance(v, int) and not isinstance(v, bool):
            nums.append(v)
    pairs = []
    for i in range(0, len(nums) - 1, 2):
        lo, hi = nums[i], nums[i + 1]
        if lo <= hi:
            pairs.append((node.args.args[0].arg if node.args.args else "x", lo, hi))
    return pairs


def lib_suite(project: Path, requirement: str) -> str:
    """lib 型：AST 签名 + 需求字符类 → 谓词端点用例（直接调函数）。"""
    src_files = [p for p in sorted(project.glob("*.py")) if not p.name.startswith("test_")]
    assert src_files, "项目无源文件"
    mod = src_files[0].stem
    char_full = all(k in requirement for k in ("大写", "小写", "数字", "特殊"))

    def base(n: int) -> str:
        core = "Ab1!" if char_full else "a"
        fill = "a" if char_full else "a"
        s = core + fill * max(0, n - len(core)) if n >= len(core) else ("A" * n if char_full else "a" * n)
        return s[:n]

    lines: list[str] = ["import os, sys",
                        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
                        f"from {mod} import *", ""]
    cases = 0
    for p in src_files:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in _predicates(tree):
            for param, lo, hi in _numeric_default_pairs(fn):
                fname = fn.name
                cases += 1
                lines += [f"def test_bnd_{fname}_len_eq_min_{lo}():",
                          f"    assert {fname}({base(lo)!r}) is True", ""]
                cases += 1
                lines += [f"def test_bnd_{fname}_len_eq_max_{hi}():",
                          f"    assert {fname}({base(hi)!r}) is True", ""]
                if lo - 1 >= 0:
                    cases += 1
                    lines += [f"def test_bnd_{fname}_len_below_{lo - 1}():",
                              f"    assert {fname}({base(lo - 1)!r}) is False", ""]
                cases += 1
                lines += [f"def test_bnd_{fname}_len_above_{hi + 1}():",
                          f"    assert {fname}({base(hi + 1)!r}) is False", ""]
    assert cases > 0, "未找到可用的谓词+数值默认参"
    return "\n".join(lines) + "\n"


def rest_suite(requirement: str) -> str:
    """REST 型：需求区间 → TestClient 端点/相邻/计价用例（corpus-003 模板）。"""
    hours = [r for r in extract_ranges(requirement) if r[1] - r[0] <= 16]
    dur = next(((lo, hi) for lo, hi in hours if 1 <= lo and hi <= 12), (1, 4))
    disc = extract_discount(requirement)
    sh_lo, sh_hi = next(((lo, hi) for lo, hi in hours if lo >= 5), (8, 20))
    rate = 10
    lines = ["import os, sys",
             "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
             "import pytest", "from fastapi.testclient import TestClient", "from app import app", "",
             "@pytest.fixture()", "def c():", "    return TestClient(app)", ""]
    # D1 靶点：时长端点（duration = end-start ∈ [lo, hi] 闭区间）
    for d, want in ((dur[0], 201), (dur[1], 201), (dur[0] - 1, 400), (dur[1] + 1, 400)):
        if d < 1 or sh_lo + d > sh_hi:
            continue
        lines += [f"def test_bnd_booking_duration_{d}h_want_{want}(c):",
                  "    r = c.post('/rooms', json={'name': 'R', 'capacity': 5, 'hourly_rate': %d})" % rate,
                  "    assert r.status_code == 201, r.text",
                  "    rid = r.json()['id']",
                  f"    b = c.post('/bookings', json={{'room_id': rid, 'date': '2030-01-01',"
                  f" 'start_hour': {sh_lo}, 'end_hour': {sh_lo + d}}})",
                  f"    assert b.status_code == {want}, b.text", ""]
    # D4 靶点：相邻时段不算重叠（结束==开始）
    lines += ["def test_bnd_adjacent_no_conflict(c):",
              "    r = c.post('/rooms', json={'name': 'R', 'capacity': 5, 'hourly_rate': %d})" % rate,
              "    rid = r.json()['id']",
              "    a = c.post('/bookings', json={'room_id': rid, 'date': '2030-01-02',"
              f" 'start_hour': {sh_lo}, 'end_hour': {sh_lo + 1}}})",
              "    assert a.status_code == 201, a.text",
              "    b = c.post('/bookings', json={'room_id': rid, 'date': '2030-01-02',"
              f" 'start_hour': {sh_lo + 1}, 'end_hour': {sh_lo + 2}}})",
              "    assert b.status_code == 201, b.text", ""]
    # D2 靶点：计价阈值（时长达到阈值打 Y 折）
    if disc:
        thr, factor = disc
        total_thr = round(rate * thr * factor)
        total_below = round(rate * (thr - 1) * 1.0) if thr > 1 else None
        lines += [f"def test_bnd_price_discount_at_{thr}h(c):",
                  "    r = c.post('/rooms', json={'name': 'R', 'capacity': 5, 'hourly_rate': %d})" % rate,
                  "    rid = r.json()['id']",
                  f"    b = c.post('/bookings', json={{'room_id': rid, 'date': '2030-01-03',"
                  f" 'start_hour': {sh_lo}, 'end_hour': {sh_lo + thr}}})",
                  "    assert b.status_code == 201, b.text",
                  f"    assert b.json()['total'] == {total_thr}, b.text", ""]
        if total_below is not None:
            lines += [f"def test_bnd_price_no_discount_below_{thr}h(c):",
                      "    r = c.post('/rooms', json={'name': 'R', 'capacity': 5, 'hourly_rate': %d})" % rate,
                      "    rid = r.json()['id']",
                      f"    b = c.post('/bookings', json={{'room_id': rid, 'date': '2030-01-04',"
                      f" 'start_hour': {sh_lo}, 'end_hour': {sh_lo + thr - 1}}})",
                      "    assert b.status_code == 201, b.text",
                      f"    assert b.json()['total'] == {total_below}, b.text", ""]
    return "\n".join(lines) + "\n"


def fp_suite(requirement: str) -> str:
    """干净项目 FP 套件：区间内 → 201；区间外 → 非 201 且非 5xx（契约只保证成功语义）。"""
    code_lo, code_hi = (int(m.group(1)), int(m.group(2))) if (
        m := RE_CODE_RANGE.search(requirement)) else (2, 8)
    pct_lo, pct_hi = (int(m.group(1)), int(m.group(2))) if (
        m := RE_PCT_RANGE.search(requirement)) else (1, 100)
    lines = ["import os, sys",
             "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
             "import pytest", "from fastapi.testclient import TestClient", "from app import app", "",
             "@pytest.fixture()", "def c():", "    return TestClient(app)", "",
             "def _mk(c, code, percent):",
             "    return c.post('/coupons', json={'code': code, 'percent': percent,"
             " 'min_total': 100})", ""]
    for v in (code_lo, code_hi):
        lines += [f"def test_bnd_code_len_{v}_in_range_ok(c):",
                  f"    r = _mk(c, {'A' * v!r}, 10)",
                  "    assert r.status_code == 201, r.text", ""]
    for v in (code_lo - 1, code_hi + 1):
        if v < 0:
            continue
        lines += [f"def test_bnd_code_len_{v}_out_range_rejected(c):",
                  f"    r = _mk(c, {'A' * v!r}, 10)",
                  "    assert r.status_code != 201 and r.status_code < 500, r.text", ""]
    for v in (pct_lo, pct_hi):
        lines += [f"def test_bnd_percent_{v}_in_range_ok(c):",
                  f"    r = _mk(c, 'AB', {v})",
                  "    assert r.status_code == 201, r.text", ""]
    for v in (pct_lo - 1, pct_hi + 1):
        if v < 0:
            continue
        lines += [f"def test_bnd_percent_{v}_out_range_rejected(c):",
                  f"    r = _mk(c, 'AB', {v})",
                  "    assert r.status_code != 201 and r.status_code < 500, r.text", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["lib", "rest", "fp"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--requirement", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    req = Path(a.requirement).read_text(encoding="utf-8")
    src = {"lib": lambda: lib_suite(Path(a.project), req),
           "rest": lambda: rest_suite(req),
           "fp": lambda: fp_suite(req)}[a.mode]()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f = out / "test_boundary_auto.py"
    f.write_text(src, encoding="utf-8")
    compile(src, str(f), "exec")            # 生成物必须可编译
    print(f"[boundary-v0] {a.mode} → {f}（{src.count('def test_')} 条用例）")
    return 0


if __name__ == "__main__":
    main()
