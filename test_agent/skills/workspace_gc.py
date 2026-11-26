# -*- coding: utf-8 -*-
"""工作区自动回收（总包 B7，2026-11-27）。

启动清扫：删除 data/workspaces/task_* 中同时满足三条件的目录——
①对应任务已是终态（done/failed/cancelled）；
②目录 mtime 距今 >14 天（近两周内的可能被复演/对账引用，不动）；
③存在验证冻结快照（data/verify_freeze/task_{id}/manifest.json）——
  删了也能按快照+DB 重建（G9 物化快照三件套），才允许删。
旗标 MTA_WS_RECYCLE（默认关）：关态零行为；开态在 server startup 调一次。
诚实边界：无快照的老工作区保留（删了就真没了——不做没有退路的删除）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .. import config

TERMINAL = ("done", "failed", "cancelled")
AGE_S = 14 * 24 * 3600


def recycle_stale_workspaces(data_dir: Path | None = None,
                             now: float | None = None,
                             age_s: int = AGE_S) -> dict:
    """清扫入口。返回 {"removed": [task_id], "kept": n, "skipped": {原因: n}}。
    纯函数式（路径/时钟可注入），server startup 与单测共用。"""
    data_dir = Path(data_dir) if data_dir else Path(config.DATA_DIR)
    ws_root = data_dir / "workspaces"
    now = now if now is not None else time.time()
    out: dict = {"removed": [], "kept": 0,
                 "skipped": {"non_terminal": 0, "recent": 0, "no_snapshot": 0,
                             "unknown_task": 0}}
    if not ws_root.exists():
        return out
    import sqlite3
    conn = sqlite3.connect(
        f"file:{(data_dir / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for d in sorted(ws_root.glob("task_*")):
            if not d.is_dir():
                continue
            stem = d.name[len("task_"):]
            if not stem.isdigit():
                out["skipped"]["unknown_task"] += 1
                continue
            tid = int(stem)
            row = conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
            if row is None or row["status"] not in TERMINAL:
                out["skipped"]["non_terminal"] += 1
                continue
            try:
                mtime = d.stat().st_mtime
            except OSError:
                out["skipped"]["unknown_task"] += 1
                continue
            if now - mtime <= age_s:
                out["skipped"]["recent"] += 1
                continue
            snap = data_dir / "verify_freeze" / f"task_{tid}" / "manifest.json"
            if not snap.exists():
                out["skipped"]["no_snapshot"] += 1     # 无退路不删（诚实边界）
                continue
            shutil.rmtree(d, ignore_errors=True)
            out["removed"].append(tid)
    finally:
        conn.close()
    out["kept"] = len(list(ws_root.glob("task_*"))) if ws_root.exists() else 0
    return out


def maybe_recycle_on_startup() -> dict | None:
    """server startup 调用点：旗标关返回 None（零行为）。"""
    if not getattr(config, "WORKSPACE_RECYCLE_ENABLED", False):
        return None
    r = recycle_stale_workspaces()
    if r["removed"]:
        import logging
        logging.getLogger(__name__).warning(
            "工作区回收（B7）：已删 %s（终态>14天且有冻结快照）", r["removed"])
    return r
