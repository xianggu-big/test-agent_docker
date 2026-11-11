"""run_grid GRID 契约 + aggregate_grid token 成本聚合单测。
锁死语义：每格恰好一个开关且开关真实存在于 config、格值≠默认值（§3「每次只动一格」
纪律的静态可查形式）、V8/V9 消融格（X6/X7）入口不被误删误改、token 聚合的缺席语义
（老任务无 llm_calls 行=不计入均值，如实标注）。跑格本身依赖 LLM 与真实库，不在此测。"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from run_grid import GRID  # noqa: E402
from aggregate_grid import format_token_cell, load_token_costs  # noqa: E402
from test_agent import config  # noqa: E402


def test_every_cell_is_single_switch_on_config():
    """X1 默认组必须零开关；其余每格恰好一个开关、开关存在于 config、格值≠默认值。"""
    assert GRID["X1"] == {}, "X1 是默认组（校准/复跑用），不许携带开关"
    for cell, overrides in GRID.items():
        if cell == "X1":
            continue
        assert len(overrides) == 1, f"{cell} 违反单开关纪律：{overrides}"
        (name, value), = overrides.items()
        assert hasattr(config, name), f"{cell} 引用不存在的开关 {name}"
        assert value != getattr(config, name), f"{cell} 的 {name}={value} 与默认值相同（空格子）"


def test_v8_v9_ablation_cells_wired():
    """X6=V8 证人继续补测、X7=V9 交叉语义审——两个消融格入口钉死（待余额复跑的既定队列）。"""
    assert GRID["X6"] == {"WITNESS_CONTINUE_ENABLED": True}
    assert GRID["X7"] == {"GEN_CROSS_REVIEW_ENABLED": True}
    # 两格默认必须关：消融才有对照意义，也与「开关默认关=零 LLM 调用口径」一致
    assert config.WITNESS_CONTINUE_ENABLED is False
    assert config.GEN_CROSS_REVIEW_ENABLED is False


def test_three_source_ablation_cells_wired():
    """三源注入消融格入口钉死（docs/领域知识注入调研-20261031.md）：
    XC=C docstring（df8d573 先行）/XA=A repo 调用形态/X9=B 陷阱库，三格默认全关。"""
    assert GRID["XC"] == {"DOCSTRING_GROUNDING_ENABLED": True}
    assert GRID["XA"] == {"USAGE_EXAMPLES_ENABLED": True}
    assert GRID["X9"] == {"PITFALL_KB_ENABLED": True}
    assert config.DOCSTRING_GROUNDING_ENABLED is False
    assert config.USAGE_EXAMPLES_ENABLED is False
    assert config.PITFALL_KB_ENABLED is False


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE llm_calls (task_id INT, total_tokens INT)")
    return conn


def test_load_token_costs_groups_by_task():
    conn = _mem_conn()
    conn.executemany("INSERT INTO llm_calls VALUES (?,?)",
                     [(1, 100), (1, 50), (2, 200)])
    costs = load_token_costs(conn)
    assert costs == {1: (2, 150), 2: (1, 200)}
    assert 3 not in costs  # 无行任务缺席，不造 0


def test_format_token_cell_attends_absence():
    full = [{"tokens": 100}, {"tokens": 300}]
    assert format_token_cell(full) == "200"
    partial = [{"tokens": 100}, {"tokens": None}]
    assert format_token_cell(partial) == "100（1/2）"
    assert format_token_cell([{"tokens": None}]) == "—（无台账）"


# ---------- F2 阶段 1 #1：C3 缺口回灌跨语言（适配器 3 列覆盖表解析） ----------

def test_per_file解析器_兼容适配器三列格式():
    """go/java/php 适配器统一文本的覆盖表是 3 列（file miss pct% missing）；
    旧 _ROW 只认 pytest 4 列 → 三语言 per_file 恒空 → C3 缺口回灌从未生效。
    修复后双格式都解析，且 4 列优先（歧义防御）。"""
    from test_agent.graph.nodes import _parse_per_file_coverage
    go_text = (
        "---------- coverage: platform coverprofile ----------\n"
        f"{'Name':<44}{'Miss':>6}{'Cover':>8}   Missing\n"
        f"{'shop/order.go':<44}{'>6':>0}"  # 占位防手误，下面用真实行
    )
    go_text = (
        "---------- coverage: platform coverprofile ----------\n"
        f"{'Name':<44}{'Miss':>6}{'Cover':>8}   Missing\n"
        f"{'shop/order.go':<44}{2:>6}{50:>7}%   14,15\n"
        f"{'TOTAL':<44}{'':>6}{50:>7}%\n")
    rows = _parse_per_file_coverage(go_text)
    assert len(rows) == 1
    r = rows[0]
    assert r["file"] == "shop/order.go" and r["miss"] == 2 and r["cover"] == 50
    assert r["missing"] == "14,15"
    # pytest 4 列（Python 原生）不回归
    py_text = f"{'calc.py':<28}{9:>7}{2:>6}{78:>6}%   6,9"
    rows2 = _parse_per_file_coverage(py_text)
    assert len(rows2) == 1 and rows2[0]["stmts"] == 9 and rows2[0]["miss"] == 2 \
        and rows2[0]["cover"] == 78


def test_C3提示词段_适配器缺口行渲染():
    """回灌链路末端：适配器形态的 per_file 行进 build_user_prompt 出『上一轮覆盖缺口』段。"""
    from test_agent.skills.prompt_library import build_user_prompt
    gaps = [{"file": "shop/order.go", "missing": "14,15", "cover": 50}]
    p = build_user_prompt([("order.go", "package shop")], "需求", gaps, None)
    assert "上一轮覆盖缺口" in p and "shop/order.go" in p and "14,15" in p
