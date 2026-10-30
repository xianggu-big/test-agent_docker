"""回填 L2 调用图快照（跨级溯源代码位置的数据源；零 Docker / 零 LLM / 只读工作区）。

背景（2026-10-30 实测）：`handler_edges` 从调用图 key 推端点根，handler 无出边时
整条端点丢根（task_151 四个端点 call_edges=0）。本脚本用 `collect_functions`
函数目录（含无出边 handler）+ `route_call_edges` 重建快照，老任务无需重跑 L2。

用法：
  python scripts/backfill_callgraph.py 151
  python scripts/backfill_callgraph.py --all     # 所有有端点清单的任务
  python scripts/backfill_callgraph.py 151 --dry-run
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config
from test_agent.skills.callgraph import (build_call_edges, collect_functions,
                                         route_call_edges)
from test_agent.store.db import get_conn, init_db


def _task_ids(conn, args: list[str]) -> list[int]:
    if "--all" in args:
        return [r["task_id"] for r in conn.execute(
            "SELECT DISTINCT task_id FROM interface_endpoints ORDER BY task_id")]
    ids = [int(a) for a in args if a.isdigit()]
    if not ids:
        raise SystemExit(__doc__)
    return ids


def backfill(conn, task_id: int, dry_run: bool = False) -> dict:
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        return {"task_id": task_id, "status": "skipped",
                "reason": f"工作区不存在：{ws}"}
    endpoints = [dict(r) for r in conn.execute(
        "SELECT method, path, operation_id FROM interface_endpoints"
        " WHERE task_id=? ORDER BY id", (task_id,))]
    if not endpoints:
        return {"task_id": task_id, "status": "skipped",
                "reason": "无端点清单（L2 未产出 interface_endpoints）"}
    edges = route_call_edges(endpoints, build_call_edges(ws), collect_functions(ws))
    if not dry_run:
        conn.execute("DELETE FROM call_edges WHERE task_id=?", (task_id,))
        for e in edges:
            conn.execute(
                """INSERT INTO call_edges(task_id, caller, callee, depth, file, lineno)
                   VALUES (?,?,?,?,?,?)""",
                (task_id, e["caller"], e["callee"], e["depth"],
                 e.get("file"), e.get("lineno")))
        conn.commit()
    return {"task_id": task_id, "status": "ok", "edges": len(edges),
            "routes": len({e["caller"] for e in edges if e["caller"].startswith("route:")}),
            "dry_run": dry_run}


def main() -> int:
    args = sys.argv[1:]
    init_db()                      # 老库补 call_edges.file/lineno 列（幂等）
    conn = get_conn()
    try:
        out = [backfill(conn, tid, "--dry-run" in args) for tid in _task_ids(conn, args)]
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
