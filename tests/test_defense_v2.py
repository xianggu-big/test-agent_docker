# -*- coding: utf-8 -*-
"""防御矩阵 v2（docs/防御矩阵-prompt与非prompt措施-20261105.md §四）：
P1 环境契约统一段 + P2 反惯例正例锚定（提示词层）+
N1 parametrize 静态闸 + N2 mock 被测模块静态闸（闸层）。
旗标 DEFENSE_V2_ENABLED 默认关=行为逐字节不变（基线保护），测试逐项锁死开/关两态。"""
import textwrap
from pathlib import Path

from test_agent import config
from test_agent.skills.gen_tests import (_static_filter, _decorator_is_parametrize,
                                         _patch_target_hits)
from test_agent.skills.prompt_library import build_user_prompt


def _mkws(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


# ---------- P1/P2：提示词接线 ----------

def test_prompt_defense_v2_on注入契约与对照():
    conds = [{"condition": f"条件{i}", "priority": "高"} for i in range(5)]
    p = build_user_prompt([("calc.py", "def add(a, b):\n    return a + b\n")],
                          "加法需求", conditions=conds, defense_v2=True)
    assert "执行环境契约" in p
    assert "本轮最多生成 5 条用例" in p            # max_cases 按 conditions 数动态填
    assert "正误对照" in p
    assert "禁止 @pytest.mark.parametrize" in p    # P1 第 6 条
    assert "禁止 mock 被测模块本身" in p            # P1 第 7 条
    assert "❌" in p and "✅" in p                  # P2 对照示例形态


def test_prompt_defense_v2_max_cases_上限与降采样同源():
    conds = [{"condition": f"条件{i}"} for i in range(43)]
    p = build_user_prompt([("calc.py", "def add(a, b):\n    return a + b\n")],
                          "需求", conditions=conds, defense_v2=True)
    assert "本轮最多生成 10 条用例" in p  # 2026-11-08 用户指令 GEN_MAX_CASES=10


def test_prompt_defense_v2_off_逐字节不变():
    src = [("calc.py", "def add(a, b):\n    return a + b\n")]
    conds = [{"condition": "条件1"}]
    assert build_user_prompt(src, "需求", conditions=conds) == \
        build_user_prompt(src, "需求", conditions=conds, defense_v2=False)
    assert "执行环境契约" not in build_user_prompt(src, "需求", conditions=conds)


# ---------- N1：参数化装饰器静态闸 ----------

def test_n1_parametrize_旗标开整案剔除(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", True)
    ws = _mkws(tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"})
    cases = [{"name": "t1", "code": textwrap.dedent("""
        import pytest
        from calc import add

        @pytest.mark.parametrize("a,b,exp", [(1, 2, 3), (2, 3, 5)])
        def test_t1(a, b, exp):
            assert add(a, b) == exp
    """)}]
    ok, rejected = _static_filter(cases, ws)
    assert not ok and any("参数化" in r for r in rejected)


def test_n1_旗标关行为不变(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", False)
    ws = _mkws(tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"})
    cases = [{"name": "t1", "code": textwrap.dedent("""
        import pytest
        from calc import add

        @pytest.mark.parametrize("a,b,exp", [(1, 2, 3)])
        def test_t1(a, b, exp):
            assert add(a, b) == exp
    """)}]
    ok, rejected = _static_filter(cases, ws)
    assert ok and not rejected          # 关=旧行为逐字节不变（基线保护）


def test_n1_判定器_多形态():
    import ast
    for dec in ("pytest.mark.parametrize(\"x\", [(1,)])", "parameterized.expand([(1,)])",
                "parametrize(\"x\", [(1,)])"):
        tree = ast.parse(f"@{dec}\ndef t():\n    pass\n")
        assert _decorator_is_parametrize(tree.body[0].decorator_list[0]), dec
    tree = ast.parse("@functools.cache\ndef t():\n    pass\n")
    assert not _decorator_is_parametrize(tree.body[0].decorator_list[0])   # 不误杀


# ---------- N2：mock 被测模块静态闸 ----------

CALC_WS = {"calc.py": "def add(a, b):\n    return a + b\n"}


def test_n2_四种命中形态_旗标开(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", True)
    ws = _mkws(tmp_path, CALC_WS)
    forms = {
        "字符串patch": "from unittest import mock\nfrom calc import add\n"
                       "def test_t():\n    mock.patch(\"calc.add\")\n",
        "对象patch.object": "from unittest import mock\nimport calc\n"
                            "def test_t():\n    mock.patch.object(calc, \"add\")\n",
        "monkeypatch对象": "import calc\n"
                           "def test_t(monkeypatch):\n    monkeypatch.setattr(calc, \"add\", lambda: 1)\n",
        "monkeypatch字符串": "def test_t(monkeypatch):\n    monkeypatch.setattr(\"calc.add\", lambda: 1)\n",
    }
    for label, code in forms.items():
        ok, rejected = _static_filter([{"name": "t1", "code": code}], ws)
        assert not ok, label
        assert any("mock 被测模块" in r for r in rejected), label


def test_n2_mock外部依赖放行不误杀(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", True)
    ws = _mkws(tmp_path, CALC_WS)
    code = textwrap.dedent("""
        from unittest import mock
        from calc import add

        def test_t():
            with mock.patch("requests.get") as m:      # 字符串形态无需 import 目标模块
                with mock.patch("time.sleep"):
                    assert add(1, 2) == 3
    """)
    ok, rejected = _static_filter([{"name": "t1", "code": code}], ws)
    assert ok and not rejected          # mock 网络/时钟=合法外部依赖 mock


def test_n2_旗标关行为不变(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", False)
    ws = _mkws(tmp_path, CALC_WS)
    code = "from unittest import mock\nfrom calc import add\n" \
           "def test_t():\n    mock.patch(\"calc.add\")\n"
    ok, rejected = _static_filter([{"name": "t1", "code": code}], ws)
    assert ok and not rejected          # 关=旧行为逐字节不变


def test_n2_判定器_裸patch命中被测模块():
    import ast
    tree = ast.parse("import calc\npatch(calc, 'add')\n")   # 裸 patch(mod,…) 同属篡改被测单元
    assert _patch_target_hits(tree, {"calc": {"add"}})


def test_n2_判定器_命中清单可读():
    import ast
    tree = ast.parse("from unittest import mock\nmock.patch('calc.add')\n")
    hits = _patch_target_hits(tree, {"calc": {"add"}})
    assert hits and "patch" in hits[0]
    assert not _patch_target_hits(tree, {"other": {"x"}})   # 非被测模块不命中
