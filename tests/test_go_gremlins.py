# -*- coding: utf-8 -*-
"""gremlins 变异解析单测（任务卡-F2 阶段 1 #9）。真机锚点=go-shop 手写套件实测输出。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.go_gremlins import kill_rate, parse_gremlins

_REAL = """Starting...
Gathering coverage... done in 5.12s
      KILLED ARITHMETIC_BASE at order.go:19:11
       LIVED CONDITIONALS_BOUNDARY at order.go:8:11
      KILLED CONDITIONALS_NEGATION at order.go:8:11
      KILLED CONDITIONALS_NEGATION at order.go:11:11
       LIVED CONDITIONALS_BOUNDARY at order.go:11:11

Mutation testing completed in 1 seconds 995 milliseconds
Killed: 3, Lived: 2, Not covered: 0
Timed out: 0, Not viable: 0, Skipped: 0
Test efficacy: 60.00%
Mutator coverage: 100.00%"""


def test_真实输出解析_全字段():
    p = parse_gremlins(_REAL)
    assert (p["killed"], p["lived"], p["not_covered"]) == (3, 2, 0)
    assert p["efficacy"] == 60.0 and p["mutator_coverage"] == 100.0
    assert len(p["lived_mutations"]) == 2
    assert p["lived_mutations"][0] == {"kind": "CONDITIONALS_BOUNDARY",
                                       "file": "order.go", "line": 8}
    assert kill_rate(p) == 0.6


def test_无摘要行_诚实空():
    p = parse_gremlins("ERROR: unknown command\n")
    assert p["killed"] is None and kill_rate(p) is None
    assert p["lived_mutations"] == []


def test_kill_rate_零变异体_None():
    assert kill_rate({"killed": 0, "lived": 0}) is None
    assert kill_rate({"killed": 1, "lived": 0}) == 1.0
