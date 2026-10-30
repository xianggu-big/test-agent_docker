# -*- coding: utf-8 -*-
"""T012 一次性驱动：A1/B1/C1 三格 × n=3 复跑（corpus-001）+ grade 阅卷。
跑完产物：grade_task_<tid>.json（data/corpus/corpus-001/）+ stdout 汇总。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CELLS = ["A1", "B1", "C1"]
N = 3
CORPUS = "corpus-001"


def main() -> int:
    ok = fail = 0
    for cell in CELLS:
        for i in range(1, N + 1):
            print(f"[t012] {cell} r{i}/3 启动 ...", flush=True)
            r = subprocess.run([sys.executable, "scripts/run_grid.py", cell, CORPUS],
                               cwd=ROOT, capture_output=True, text=True, timeout=1800)
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            print(f"[t012] {cell} r{i} → exit={r.returncode} | {tail[0][:160]}", flush=True)
            ok += int(r.returncode == 0)
            fail += int(r.returncode != 0)
    print(f"[t012] 走图完成：成功 {ok} / 失败 {fail}，开始阅卷 ...", flush=True)

    from test_agent.store.db import get_conn
    conn = get_conn()
    graded = 0
    for cell in CELLS:
        rows = conn.execute(
            "SELECT t.id FROM test_tasks t JOIN projects p ON p.id=t.project_id "
            "WHERE p.name=? ORDER BY t.id", (f"grid-{cell}-{CORPUS}",)).fetchall()
        tids = [r["id"] for r in rows][-N:]  # 每格最近 n 轮（今晚批次）
        for tid in tids:
            g = subprocess.run([sys.executable, "scripts/grade_corpus.py", str(tid), CORPUS],
                               cwd=ROOT, capture_output=True, text=True, timeout=600)
            last = (g.stdout or "").strip().splitlines()[-1:] or [""]
            print(f"[t012] grade {cell}#{tid} → exit={g.returncode} | {last[0][:140]}", flush=True)
            graded += int(g.returncode == 0)
    print(f"[t012] 阅卷完成 {graded} 份；汇总请跑 python scripts/aggregate_grid.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
