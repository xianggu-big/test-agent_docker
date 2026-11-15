"""卡 5 测试锁：八指标聚合器（2026-11-15）。评测器自身也是被测对象（R8 纪律）——
聚合口径的分母/分子/跨轮去重必须锁死。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_metrics import aggregate


def test_aggregate_denominators():
    rows = [
        {"task": 1, "env_ok": 1, "gen_ok": 1, "gen_cases": 10, "gen_executed": 5,
         "id_hit": 1, "coverage": 0.5, "tokens": 100, "detected": 1,
         "gt_hit": 1, "clean_fp": 0, "minutes": 3.0},
        {"task": 2, "env_ok": 0, "gen_ok": 0, "gen_cases": 0, "gen_executed": 0,
         "coverage": None, "tokens": 50, "detected": 0, "minutes": 9.0},
        {"task": 3, "env_ok": 1, "gen_ok": 1, "gen_cases": 4, "gen_executed": 4,
         "id_hit": 0, "coverage": 1.0, "tokens": 30, "detected": 0,
         "clean_fp": 1, "minutes": None},
    ]
    agg = aggregate(rows)
    assert agg["n_tasks"] == 3
    assert agg["环境构建成功率"] == round(2 / 3, 4), "分母=全部终态任务"
    assert agg["生成成功率"] == round(2 / 3, 4)
    assert agg["AI用例执行覆盖"] == round((0.5 + 1.0) / 2, 4), "gen_cases=0 的任务不进均值分母"
    assert agg["识别准确率"] == 0.5, "有真值标注的才进识别分母"
    assert agg["检出率_GT"] == 1.0 and agg["误报率_干净语料"] == 0.5
    assert agg["平均覆盖率"] == 0.75 and agg["平均耗时分钟"] == 6.0
    assert agg["总Token"] == 180


def test_aggregate_empty():
    agg = aggregate([])
    assert agg["n_tasks"] == 0 and agg["识别准确率"] is None and agg["总Token"] == 0


def test_readonly_connection_guarantee():
    """真实库只读红线：_ro_conn 走 file:ro URI，写操作必须被 SQLite 拒绝。"""
    from eval_metrics import _ro_conn
    import sqlite3
    c = _ro_conn()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS _t(x)")
        raise AssertionError("只读连接竟允许写入——红线 2 的机械保证失效")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e).lower()
    finally:
        c.close()
