"""level_supervisor —— 四级串行任务接力（D9 定稿：A 路线，Python 编排层）。

    run_task_levels(task_id)
        ├─ L1 leg：run_task() 原样（9 节点图 + R-Loop + review，零改动）
        ├─ test_levels 记账：每级一行，trace_from 串成跨级追溯链（四级 RTM 的链头）
        └─ L2 leg：run_l2_task()（本级独立 StateGraph）

不选 LangGraph 外层嵌套（B 路线）的原因见蓝图 §11-D9：串行接力按任务天然隔离、
排错直接、复用全部现有入口；论文分层编排图把 supervisor 画作外层节点即可。

向后兼容：LEVELS_ENABLED=False 时 API 下发仍只跑 L1（run_task），行为逐字节不变。
"""
import json
import logging

from .. import config
from ..store.db import get_conn, init_db
from .builder import run_task
from .l2 import _now, run_l2_task

log = logging.getLogger(__name__)


def _leg_upsert(conn, task_id: int, level: str, **fields) -> int:
    """test_levels upsert（UNIQUE(task,level)）：重跑以本轮为准，返回 leg id。"""
    row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level=?",
                       (task_id, level)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE test_levels SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        conn.commit()
        return row["id"]
    cols = ", ".join(fields)
    cur = conn.execute(
        f"INSERT INTO test_levels(task_id, level, {cols})"
        f" VALUES (?, ?, {', '.join('?' for _ in fields)})",
        (task_id, level, *fields.values()))
    conn.commit()
    return cur.lastrowid


def _leg_update(conn, leg_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE test_levels SET {sets} WHERE id=?", (*fields.values(), leg_id))
    conn.commit()


def run_task_levels(task_id: int, levels: tuple[str, ...] = ("L1", "L2")) -> dict:
    """串行接力入口：L1 原样跑完 → L2 接力 → L3 半自动（LEVELS_L3_ENABLED 门，默认关）。"""
    init_db()
    conn = get_conn()
    try:
        task = conn.execute("SELECT confirmed FROM test_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")
        if task["confirmed"] != 1:
            raise RuntimeError("任务未确认（confirmed=0），禁止下发执行 —— fail-closed 确认门")

        l1_leg = _leg_upsert(conn, task_id, "L1", strategy="full", status="running",
                             started_at=_now())
        l1_final = run_task(task_id)                     # L1 原样，零改动
        l1_ok = bool(l1_final.get("done")) and l1_final.get("verdict") in ("pass", "stop")
        _leg_update(conn, l1_leg, status="done" if l1_ok else "failed",
                    verdict=str(l1_final.get("verdict")),
                    stop_reason=l1_final.get("stop_reason"),
                    summary=json.dumps(
                        {k: l1_final.get(k) for k in ("passed", "failed", "coverage_line",
                                                      "verdict") if l1_final.get(k) is not None},
                        ensure_ascii=False),
                    finished_at=_now())
        if "L2" not in levels:
            return {"L1": l1_final}

        l1_leg_row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level='L1'",
                                  (task_id,)).fetchone()
        l2_final = run_l2_task(task_id, trace_from=l1_leg_row["id"] if l1_leg_row else None)
        if "L3" not in levels or not config.LEVELS_L3_ENABLED:
            return {"L1": l1_final, "L2": l2_final}
        l2_leg_row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level='L2'",
                                  (task_id,)).fetchone()
        from .l3 import run_l3_task  # 延迟导入：L3 关闭时零加载
        l3_final = run_l3_task(task_id, trace_from=l2_leg_row["id"] if l2_leg_row else None)
        return {"L1": l1_final, "L2": l2_final, "L3": l3_final}
    finally:
        conn.close()
