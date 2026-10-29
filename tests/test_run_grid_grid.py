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
