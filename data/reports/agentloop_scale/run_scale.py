# -*- coding: utf-8 -*-
"""AgentLoop 大规模测试驱动器（任务四 AL-7，2026-11-26 测试任务报告）——走生产 server API 落生产库。

从 data/reports/t10_measure/run_batch.py 拷贝改造（原文件不动）：--n 每项目格数、
--parking 停车线、--tag 版本标注（默认 v3）；停车线/高峰闸/账本四表齐三项闸/断点续跑全保留。

用法：
  python -I run_scale.py --n 15 --parking 3.4 --tag v3
  python -I run_scale.py --n 15 --dry          # 只列计划不烧钱
留档：data/reports/agentloop_scale/cells/scale_<tag>_*.json + 每格结算 JSON
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
PROD_DB = ROOT / "data" / "test_agent.db"
HERE = Path(__file__).parent
CELLS = HERE / "cells"
PRICE = {"deepseek-chat": (1, 2), "deepseek-reasoner": (4, 16)}
PEAK_START = ("09:00", "14:00")   # 高峰段起点
PROJECTS = {
    "js-calc": {"repo": "data/demo/js-calc", "baseline": 1156},
    "go-api-book": {"repo": "data/demo/go-api-book", "baseline": 1159},
    "coupon-api": {"repo": "data/demo/L2场景-coupon-api", "baseline": 1155},
}


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e


def load_req(baseline: int) -> str:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                            (baseline,)).fetchone()[0]
    finally:
        conn.close()


def cost_of_task(tid: int) -> float:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT model, prompt_tokens, completion_tokens FROM llm_calls "
                            "WHERE task_id=?", (tid,)).fetchall()
    finally:
        conn.close()
    yuan = 0.0
    for model, p, cm in rows:
        price = PRICE.get(model, PRICE["deepseek-chat"])
        yuan += (p or 0) / 1e6 * price[0] + (cm or 0) / 1e6 * price[1]
    return round(yuan, 4)


def spent_so_far(tag: str) -> float:
    total = 0.0
    for f in CELLS.glob(f"scale_{tag}_*.json"):
        total += json.load(open(f, encoding="utf-8")).get("cost_yuan_est", 0.0)
    return round(total, 4)


def in_peak() -> bool:
    now = time.strftime("%H:%M")
    return PEAK_START[0] <= now < PEAK_START[1]


def four_table_check(tid: int) -> dict:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT id, status, prompt_version, started_ms, ended_ms, "
                           "summary_json FROM agent_runs WHERE task_id=? ORDER BY id DESC "
                           "LIMIT 1", (tid,)).fetchone()
        if run is None:
            return {"ok": False, "reason": "无 run 行"}
        steps = conn.execute("SELECT COUNT(*) AS n, "
                             "SUM(CASE WHEN llm_call_id IS NOT NULL THEN 1 ELSE 0 END) AS j_llm "
                             "FROM agent_steps WHERE run_id=?", (run["id"],)).fetchone()
        llm_join = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_steps s JOIN llm_calls l ON l.id=s.llm_call_id "
            "WHERE s.run_id=?", (run["id"],)).fetchone()["n"]
        dangling_inv = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_steps s "
            "LEFT JOIN skill_invocations si ON si.id=s.invocation_id "
            "WHERE s.run_id=? AND s.invocation_id IS NOT NULL AND si.id IS NULL",
            (run["id"],)).fetchone()["n"]
        rep = conn.execute("SELECT duration_ms FROM test_reports WHERE task_id=? "
                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        checks = {
            "run_row": run is not None,
            "run_closed": bool(run and run["status"] in ("done", "failed", "cancelled")),
            "prompt_version": bool(run and run["prompt_version"]),
            "time_window": bool(run and run["started_ms"] and run["ended_ms"]),
            "steps_rows": bool(steps and steps["n"] > 0),
            "llm_join_full": bool(steps and (steps["j_llm"] or 0) == llm_join),
            "inv_join": dangling_inv == 0,
            "report_duration": bool(rep and rep["duration_ms"] is not None),
            "summary_replayable": bool(run and run["summary_json"]),
        }
        return {"ok": all(checks.values()), "checks": checks, "run_id": run["id"]}
    finally:
        conn.close()


def run_cell(key: str, idx: int, tag: str, dev_token: str, caller_token: str) -> dict:
    proj = PROJECTS[key]
    req = load_req(proj["baseline"])
    t = api("POST", "/api/tasks", caller_token,
            {"repo_url": str(ROOT / proj["repo"]), "requirement": req,
             "coverage_target": 0.7, "name": f"al-{tag}-{key}-{idx}"})
    tid = t["task_id"]
    api("POST", f"/api/tasks/{tid}/confirm", dev_token)
    r = api("POST", f"/api/tasks/{tid}/run", dev_token)
    t0 = time.time()
    terminal = {"done", "failed", "cancelled"}
    row = {}
    while time.time() - t0 < 900:                     # 15 分钟格级超时
        row = api("GET", f"/api/tasks/{tid}", dev_token)
        if row.get("status") in terminal:
            break
        time.sleep(6)
    cost = cost_of_task(tid)
    ft = four_table_check(tid)
    rec = {"key": key, "idx": idx, "tag": tag, "task_id": tid, "status": row.get("status"),
           "error": (row.get("error") or "")[:200], "cost_yuan_est": cost,
           "duration_s": round(time.time() - t0, 1), "four_table": ft}
    (CELLS / f"scale_{tag}_{key}_{idx}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[cell] {key}#{idx} tid={tid} status={rec['status']} cost≈¥{cost} "
          f"四表={'PASS' if ft['ok'] else json.dumps(ft.get('checks') or ft)}", flush=True)
    return rec


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15, help="每项目格数")
    ap.add_argument("--parking", type=float, required=True, help="停车线（元）")
    ap.add_argument("--tag", default="v3")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    CELLS.mkdir(parents=True, exist_ok=True)

    def _login(user: str, pwd: str) -> str:
        body = json.dumps({"username": user, "password": pwd}).encode()
        req = urllib.request.Request(BASE + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]

    dev_token = _login("dev", "dev123")
    ledger = json.loads(urllib.request.urlopen(
        urllib.request.Request(BASE + "/api/runs?limit=1",
                               headers={"Authorization": "Bearer " + dev_token}),
        timeout=10).read())
    if not ledger.get("ledger_enabled"):
        print("[scale] 生产 server 账本未开——先以 MTA_AGENT_LOOP=1 MTA_RUN_LEDGER=1 启动")
        return 2
    caller_token = _login("caller", "caller123")

    plan = [(key, i) for key in PROJECTS for i in range(1, args.n + 1)]
    if args.smoke:
        plan = [("js-calc", 1)]
    done = {f.name for f in CELLS.glob(f"scale_{args.tag}_*.json")}
    print(f"[scale] 计划 {len(plan)} 格（tag={args.tag}）；已花费 ¥{spent_so_far(args.tag)}；"
          f"停车线 ¥{args.parking}")
    if args.dry:
        for key, idx in plan:
            mark = "已留档" if f"scale_{args.tag}_{key}_{idx}.json" in {d.name for d in CELLS.iterdir()} else "待跑"
            print(f"  {key}#{idx} {mark}")
        print("[scale] dry 完成——未烧钱")
        return 0

    for key, idx in plan:
        out = CELLS / f"scale_{args.tag}_{key}_{idx}.json"
        if out.exists():
            print(f"[skip] {key}#{idx} 已留档")
            continue
        if in_peak():
            print(f"[gate] 高峰段 {time.strftime('%H:%M')}——停新格，在途跑完即止")
            break
        if spent_so_far(args.tag) + 0.08 > args.parking:
            print(f"[gate] 触停车线 ¥{spent_so_far(args.tag)}——停止放量")
            break
        rec = run_cell(key, idx, args.tag, dev_token, caller_token)
        if not rec["four_table"]["ok"]:
            print("[scale] 四表不齐——停火取证（判据=数据完整性）")
            break
    total = spent_so_far(args.tag)
    print(f"[scale] 累计花费 ¥{total} / 停车线 ¥{args.parking}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
