# -*- coding: utf-8 -*-
"""E9/E10 每日续跑：当日波次盘点（只读，零 API）。

输出：E9 计划完成度与 500 跑上限余量 / E10 全量驱动器与 Wave 2 主力波状态 /
余额快照由调用方另行查询。结果供每日进度报告引用。
"""
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
E9 = ROOT / "data" / "reports" / "e9_full"
E10F = ROOT / "data" / "reports" / "e10_full"
E10 = ROOT / "data" / "reports" / "e10"
sys.path.insert(0, str(ROOT))


def load_plan(module_path: Path):
    spec = importlib.util.spec_from_file_location("drv", module_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def status_of(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("status")
    except Exception:  # noqa: BLE001
        return "corrupt"


def e9_report() -> dict:
    m = load_plan(E9 / "run_e9_full.py")
    plan = m.build_plan()
    c = Counter()
    pending, no_json = [], []
    for cid, _p in plan:
        st = status_of(E9 / f"{cid}.json")
        c[st if st else "no_json"] += 1
        if st != "done":
            (pending if st else no_json).append(cid)
    archived = sum(v for k, v in c.items() if k != "no_json")
    remain_runs = max(0, m.RUN_CAP - archived)
    return {"plan_total": len(plan), "archived": dict(c), "archived_n": archived,
            "pending_not_done": len(pending), "pending_list_head": pending[:20],
            "run_cap": m.RUN_CAP, "remain_runs": remain_runs}


def wave2_report() -> dict:
    f = E10 / "wave2_results.json"
    if not f.exists():
        return {"exists": False}
    recs = json.loads(f.read_text(encoding="utf-8"))
    c = Counter(r.get("status") for r in recs)
    tok = sum(int(r.get("tokens_spent") or 0) for r in recs)
    slots = {r["task_slot"] for r in recs if "task_slot" in r}
    return {"exists": True, "total": 300, "records": len(recs),
            "status": dict(c), "tokens": tok,
            "task_slots_done": len(slots),
            "last_at": max((r.get("at") or "" for r in recs), default="")}


def e10_full_report() -> dict:
    plan_ids = [f.stem for f in sorted(E10F.glob("e10a*.json"))]
    c = Counter(status_of(E10F / f"{cid}.json") for cid in plan_ids)
    tok = sum(int(json.loads((E10F / f"{cid}.json").read_text(encoding="utf-8"))
                  .get("tokens_spent") or 0) for cid in plan_ids)
    return {"plan": len(plan_ids), "status": dict(c), "tokens": tok}


def db_snapshot() -> dict:
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    t = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok "
                     "FROM llm_calls").fetchone()
    last = conn.execute("SELECT MAX(created_at) m FROM llm_calls").fetchone()["m"]
    conn.close()
    return {"calls": t["n"], "tokens": t["tok"], "last_call": last}


if __name__ == "__main__":
    print(json.dumps({"e9": e9_report(), "e10_full": e10_full_report(),
                      "e10_wave2": wave2_report(), "db": db_snapshot()},
                     ensure_ascii=False, indent=1))
