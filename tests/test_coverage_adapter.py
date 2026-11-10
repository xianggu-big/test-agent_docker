# -*- coding: utf-8 -*-
"""CoverageAdapter 统一覆盖率抽象（任务卡-F2 阶段 0）单元测试。

口径锁：5 种后端（coverage.py JSON / istanbul coverageMap / JaCoCo XML /
go coverprofile / clover XML）解析结果的 per_file 字段集逐字一致；数值正确；
格式缺分支信息时 branches_* 如实为 None（不编 0）；缺口行压缩与回灌行口径。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.coverage_adapter import (FILE_KEYS, compress_ranges,
                                                from_clover_xml,
                                                from_coverage_py_json,
                                                from_go_coverprofile,
                                                from_istanbul, from_jacoco_xml,
                                                gap_lines)

_COVPY = {"files": {"calc.py": {"executed_lines": [1, 2, 5], "missing_lines": [6, 9],
                                "summary": {"num_statements": 5, "missing_lines": 2,
                                            "num_branches": 4, "covered_branches": 3}}},
          "totals": {"num_statements": 5}}

_ISTANBUL = {"/work/src/calc.js": {
    "path": "/work/src/calc.js",
    "statementMap": {"0": {"start": {"line": 2}, "end": {"line": 2}},
                     "1": {"start": {"line": 3}, "end": {"line": 4}},
                     "2": {"start": {"line": 6}, "end": {"line": 6}}},
    "s": {"0": 1, "1": 0, "2": 1},
    "branchMap": {"0": {"type": "if-else", "locations": [
        {"start": {"line": 3}, "end": {"line": 3}},
        {"start": {"line": 4}, "end": {"line": 4}}]}},
    "b": {"0": [0, 1]},
}}

_JACOCO = """<report><package name="com/demo">
  <sourcefile name="OrderService.java">
    <line nr="3" mi="0" ci="2" mb="0" cb="0"/>
    <line nr="5" mi="2" ci="0" mb="2" cb="0"/>
    <line nr="6" mi="0" ci="1" mb="0" cb="2"/>
    <counter type="LINE" missed="2" covered="2"/>
    <counter type="BRANCH" missed="2" covered="2"/>
  </sourcefile>
</package></report>"""

_GO = ("mode: set\n"
       "shop/order.go:10.2,12.3 2 1\n"
       "shop/order.go:14.2,15.9 1 0\n"
       "shop/order.go:20.2,22.3 3 0\n")

_CLOVER = """<coverage><project>
  <file name="/work/src/Shop.php">
    <metrics statements="4" coveredstatements="3" conditionals="4" coveredconditionals="2"/>
    <line num="5" type="stmt" count="1"/>
    <line num="7" type="stmt" count="0"/>
    <line num="9" type="stmt" count="1"/>
    <line num="12" type="stmt" count="0"/>
  </file>
</project></coverage>"""


def test_五后端_per_file字段集逐字一致():
    docs = [from_coverage_py_json(_COVPY),
            from_istanbul(_ISTANBUL),
            from_jacoco_xml(_JACOCO),
            from_go_coverprofile(_GO),
            from_clover_xml(_CLOVER)]
    assert len(docs) == 5
    for d in docs:
        assert d["files"], "每个后端都应解析出文件"
        for f in d["files"]:
            assert set(f.keys()) == set(FILE_KEYS)
            assert isinstance(f["missing_lines"], list)
    assert len({json.dumps(sorted(FILE_KEYS)) for d in docs}) == 1


def test_数值正确_各后端():
    covpy = from_coverage_py_json(_COVPY)["files"][0]
    assert covpy["file"] == "calc.py" and covpy["statements"] == 5
    assert covpy["missing_lines"] == [6, 9]
    assert (covpy["branches_covered"], covpy["branches_total"]) == (3, 4)

    ist = from_istanbul(_ISTANBUL, root="/work")["files"][0]
    assert ist["file"] == "src/calc.js"                     # 去绝对路径前缀（相对工作区形态）
    assert ist["statements"] == 3 and ist["missing_lines"] == [3]
    assert (ist["branches_covered"], ist["branches_total"]) == (1, 2)

    jac = from_jacoco_xml(_JACOCO)["files"][0]
    assert jac["file"] == "com/demo/OrderService.java"      # package+sourcefile
    assert jac["missing_lines"] == [5]                      # mi>0 且 ci==0
    assert (jac["branches_covered"], jac["branches_total"]) == (2, 4)

    go = from_go_coverprofile(_GO)["files"][0]
    assert go["file"] == "shop/order.go"
    assert go["statements"] == 6                            # 2+1+3
    assert go["missing_lines"] == [14, 15, 20, 21, 22]      # 块区间展开、有序去重
    assert go["branches_covered"] is None and go["branches_total"] is None  # go 无分支

    clv = from_clover_xml(_CLOVER)["files"][0]
    assert clv["statements"] == 4 and clv["missing_lines"] == [7, 12]
    assert (clv["branches_covered"], clv["branches_total"]) == (2, 4)


def test_branches缺失格式_如实None_不编0():
    for d in (from_go_coverprofile(_GO),):
        assert all(f["branches_total"] is None for f in d["files"])


def test_totals_同口径():
    doc = from_go_coverprofile(_GO)
    assert doc["totals"]["statements"] == 6
    assert doc["totals"]["missing"] == 5


def test_compress_ranges_相邻合并():
    assert compress_ranges([14, 15, 20, 21, 22, 30]) == "14-15, 20-22, 30"
    assert compress_ranges([]) == ""


def test_gap_lines_回灌口径_缺口文件优先():
    doc = from_clover_xml(_CLOVER)
    lines = gap_lines(doc, max_files=2)
    assert any("Shop.php" in ln and "7, 12" in ln for ln in lines), lines
