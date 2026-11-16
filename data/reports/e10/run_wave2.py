# -*- coding: utf-8 -*-
"""E10 Wave 2 主力波驱动器（Arm 1 失败知识反哺：100 任务 × 3 轮 = 300 格）。

- 选格：E9 failed 语料任务（GT 已知或证人已冻结）按 id 均匀取 100；
- 每任务 3 轮独立采样（trapkb 注入全程 ON），对照组=E9 原格（零成本）；
- 指标：每轮检出数/覆盖率/通过数 → 收敛曲线（每轮信息增量）；
- 纪律：402 不降级（直接标 budget_exhausted 停波留档）/ 每格 JSON 留档 / 高峰门
  由点火时间控制（18:01 低价窗起跑）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
E10 = ROOT / "data" / "reports" / "e10"
OUT = E10 / "wave2_results.json"
RESULT_MARK = "@@RESULT@@"   # E9 worker 留档标记（勿改——Wave 1 三轮调试教训）


def pick_cells(n_tasks: int = 100) -> list[dict]:
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT t.id, p.name FROM test_tasks t JOIN projects p ON p.id=t.project_id
        WHERE p.name LIKE 'e9a%corpus%' AND t.status='failed'
        ORDER BY t.id""").fetchall()
    c.close()
    ids = [r["id"] for r in rows]
    # 均匀抽样 100（跨语料覆盖）
    if len(ids) > n_tasks:
        step = len(ids) / n_tasks
        ids = [ids[int(i * step)] for i in range(n_tasks)]
    return [{"tid": tid} for tid in ids]


def run_cell(baseline_tid: int, seq: int, round_no: int) -> dict:
    params = {
        "arm": "A1", "corpus": "corpus-003",
        "n": 2000 + seq, "combo_idx": 200 + seq,
        "flags": {"TRAPKB_V2_ENABLED": True},
        "profile": None,
    }
    ppath = E10 / f"wave2_p.json"
    ppath.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    t0 = time.time()
    rec = {"baseline_tid": baseline_tid, "round": round_no, "seq": seq}
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "data" / "reports" / "e9_full" / "run_e9_full.py"),
             "--worker", str(ppath)],
            capture_output=True, text=True, timeout=45 * 60, cwd=str(ROOT))
        out = r.stdout or ""
        line = next((ln for ln in out.splitlines()
                     if ln.startswith(RESULT_MARK)), None)
        if line:
            rec.update(json.loads(line[len(RESULT_MARK):]))
        else:
            rec.update({"status": "error",
                        "error": (r.stderr or "no result mark")[-300:],
                        "returncode": r.returncode})
    except subprocess.TimeoutExpired as e:
        rec.update({"status": "timeout", "error": str(e)[:200]})
    except Exception as e:  # noqa: BLE001
        rec.update({"status": "driver_error", "error": f"{type(e).__name__}: {e}"})
    rec["elapsed_s"] = int(time.time() - t0)
    return rec


def main() -> int:
    E10.mkdir(parents=True, exist_ok=True)
    cells = pick_cells(100)
    print(f"Arm1 主力波: {len(cells)} 任务 × 3 轮 = {len(cells)*3} 格", flush=True)
    results, t0 = [], time.time()
    done_n = 0
    for i, cell in enumerate(cells):
        for round_no in (1, 2, 3):
            rec = run_cell(cell["tid"], i * 3 + round_no, round_no)
            rec["task_slot"] = i
            results.append(rec)
            if rec.get("status") == "done":
                done_n += 1
            # 每格即落盘（断点可查）
            OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        if (i + 1) % 10 == 0:
            el = int((time.time() - t0) / 60)
            print(f"[任务 {i+1}/{len(cells)}] done {done_n}/{len(results)} "
                  f"({el} 分钟)", flush=True)
    print(f"\n收官: done {done_n}/{len(results)}，总耗时 "
          f"{int((time.time()-t0)/60)} 分钟", flush=True)
    print(f"落盘: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
