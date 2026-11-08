"""BREADTH 广度采样单测（任务卡 .autonomous-loop/e9e10_daily/task_card_breadth_5q.md）。

锁死两态：默认关=降采样旧行为逐字节不变 + plan 提示词无清单段；开=15+≤5 函数域分散补充。
"""
import tempfile
from pathlib import Path

from test_agent import config
from test_agent.skills.gen_tests import (_condition_function_domains,
                                         _downsample_conditions, source_outline)
from test_agent.skills.prompt_library import build_plan_prompt


def _conds(n: int, keyword: str = "check_length") -> list[dict]:
    return [{"condition": f"验证 {keyword} 的第{i}个行为", "priority": "中",
             "method_hint": "边界值分析"} for i in range(n)]


def _mk_ws(tmp: Path) -> Path:
    (tmp / "app.py").write_text(
        "def check_length(pwd, min_len=8, max_len=64):\n    return min_len <= len(pwd) <= max_len\n\n"
        "def strength_score(pwd):\n    return 5\n\n"
        "class AuditLog:\n    def record(self, event):\n        return event\n",
        encoding="utf-8")
    (tmp / "helper.py").write_text(
        "def format_report(score):\n    return f'score={score}'\n\n"
        "def send_notification(email):\n    return True\n", encoding="utf-8")
    return tmp


def test_breadth_default_off():
    assert config.BREADTH_SAMPLING_ENABLED is False  # 代码默认关（env 未设时）


def test_downsample_off_legacy_behavior(monkeypatch):
    monkeypatch.setattr(config, "BREADTH_SAMPLING_ENABLED", False)
    with tempfile.TemporaryDirectory() as d:
        conds, truncated = _downsample_conditions(_conds(43), Path(d), "python", 0)
    assert len(conds) == 10 and truncated == 33  # 2026-11-08 用户指令：GEN_MAX_CASES=10


def test_downsample_on_breadth_fill(monkeypatch):
    monkeypatch.setattr(config, "BREADTH_SAMPLING_ENABLED", True)
    with tempfile.TemporaryDirectory() as d:
        ws = _mk_ws(Path(d))
        # 前 15 条全挤 check_length 域；rest 里放 helper.py 的函数域条件 → 应被分散补回
        conds = _conds(15) + [
            {"condition": "验证 format_report 的空输入行为", "priority": "低",
             "method_hint": "等价类划分"},
            {"condition": "验证 send_notification 的非法邮箱行为", "priority": "低",
             "method_hint": "错误推测法"},
            {"condition": "又一条 check_length 行为", "priority": "低", "method_hint": "边界值分析"},
        ]
        out, truncated = _downsample_conditions(conds, ws, "python", 0)
    assert len(out) == 12  # 10 + 2 条广度补充（第三条 check_length 同域不补）
    assert truncated == 6  # 18 条 - 12 入集 = 6 条被截
    assert any("format_report" in c["condition"] for c in out)
    assert any("send_notification" in c["condition"] for c in out)
    assert sum(1 for c in out if "check_length" in c["condition"]) == 10  # rest 同域不补，仍 10


def test_downsample_on_cap_at_20(monkeypatch):
    monkeypatch.setattr(config, "BREADTH_SAMPLING_ENABLED", True)
    with tempfile.TemporaryDirectory() as d:
        ws = _mk_ws(Path(d))
        conds = _conds(15) + [
            {"condition": f"验证 helper{i} 函数行为", "priority": "低",
             "method_hint": "错误推测法"} for i in range(10)
        ]
        out, _truncated = _downsample_conditions(conds, ws, "python", 0)
    assert len(out) == 10  # helper{i} 不命中任何源码函数名 → 无域可补（不盲补）；cap=10


def test_condition_function_domains():
    fns = {"check_length", "strength_score"}
    got = _condition_function_domains(
        [{"condition": "验证 strength_score 边界", "method_hint": ""}], fns)
    assert got == {"strength_score"}


def test_source_outline_python(tmp_path):
    ws = _mk_ws(tmp_path)
    outline = source_outline(ws, "python")
    assert outline is not None
    assert "strength_score" in outline["functions"]
    assert "send_notification" in outline["functions"]
    assert any(f.startswith("app.py::") for f in outline["files"])
    assert any(f.startswith("helper.py::") for f in outline["files"])


def test_plan_prompt_without_modules_unchanged():
    req = "需求文本"
    old = build_plan_prompt(req, hotspots=None)
    assert "源码结构清单" not in old  # 默认关：无清单段（提示词逐字节不变口径）


def test_plan_prompt_with_modules():
    p = build_plan_prompt("需求", hotspots=None, source_modules=["app.py::check_length"])
    assert "源码结构清单" in p and "app.py::check_length" in p
    assert "不超过 3 条" in p and "至少各拆解 1 条" in p  # 广度纪律注入
