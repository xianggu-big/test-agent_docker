# -*- coding: utf-8 -*-
"""Go 静态分析层（任务卡-F2 阶段 1 #4 vet/#2 gocyclo）单元测试。

口径锁：vet 输出 file:line:col: message 结构化（多包 # 头行剔除）；gocyclo
输出（复杂度 函数 包 文件:行[:列]）解析、Average 行剔除；采集 fail-open
（容器不可达=空表不抛）；旗标 MTA_GO_STATIC 默认关。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.go_static import (collect_go_static, parse_go_vet,
                                         parse_gocyclo)


def test_parse_go_vet_结构化():
    text = (
        "# test.example.com/shop\n"
        "shop/order.go:12:5: unreachable code\n"
        "shop/order.go:20:2: result of fmt.Sprintf call not used\n"
        "多行续写不是 finding\n"
    )
    rows = parse_go_vet(text)
    assert [r["file"] for r in rows] == ["shop/order.go", "shop/order.go"]
    assert rows[0] == {"file": "shop/order.go", "line": 12, "col": 5,
                       "message": "unreachable code"}
    assert rows[1]["message"].startswith("result of fmt.Sprintf")


def test_parse_go_vet_无列号兼容_空输入():
    rows = parse_go_vet("shop/a.go:8: missing return\n")
    assert rows == [{"file": "shop/a.go", "line": 8, "col": None,
                     "message": "missing return"}]
    assert parse_go_vet("") == []
    assert parse_go_vet("# pkg\n（编译失败诊断行）") == []


def test_parse_gocyclo_热点行():
    """列序=复杂度 包 函数 文件:行（夹具实测实锤：16 hot Hot hot.go:3）。"""
    text = (
        "12 shop GetTier shop/order.go:10:1\n"
        "3 shop Add shop/order.go:22:1\n"
        "Average: 2.5\n"
    )
    rows = parse_gocyclo(text)
    assert [r["complexity"] for r in rows] == [12, 3]
    assert rows[0] == {"complexity": 12, "func": "GetTier", "pkg": "shop",
                       "file": "shop/order.go", "line": 10}
    assert all("Average" not in r["func"] for r in rows)


def test_rank映射_radon同尺():
    from test_agent.skills.go_static import complexity_rank
    assert complexity_rank(3) == "A" and complexity_rank(11) == "C" \
        and complexity_rank(26) == "F"


def test_collect_fail_open_容器异常返回空表():
    class Boom:
        def exec_in(self, *a, **kw):
            raise RuntimeError("container gone")
    out = collect_go_static(Boom(), "mta-x")
    assert out == {"vet": [], "gocyclo": []}


def test_collect_真实容器形态_注入runner假件():
    import types

    class FakeContainer:
        pass

    class FakeRunner:
        def __init__(self):
            self.calls = []

        @property
        def client(self):
            outer = self

            class C:
                containers = types.SimpleNamespace(
                    get=lambda name: FakeContainer())
            return C()

        def exec_in(self, container, cmd, timeout_s=120, workdir="/work"):
            self.calls.append(" ".join(cmd))
            if cmd and cmd[0] == "go":
                return types.SimpleNamespace(ok=True, exit_code=0, stdout="",
                                             stderr="# example.com/shop\n"
                                                    "shop/order.go:9:2: a := b is unused\n")
            return types.SimpleNamespace(ok=True, exit_code=0, stdout=(
                "12 GetTier shop shop/order.go:10:1\n"), stderr="")

    fr = FakeRunner()
    out = collect_go_static(fr, "c1", ws_pkg=".")
    assert out["vet"][0]["file"] == "shop/order.go"
    assert out["gocyclo"][0]["complexity"] == 12
    assert any(c.startswith("go vet") for c in fr.calls)   # 命令形态实锁
