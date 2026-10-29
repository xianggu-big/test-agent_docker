"""D8 补充队列（按已阅卷数补齐）：corpus-001 的 A1/B1/X6/X7 补到 graded n>=3。

与 run_d8_queue 的区别：以 grade_task*.json 存在且含 recall 为计数口径（套件全过=0.0 可测，
0 用例退化不可阅需重试）；跑间 sleep 防限流（连发触发 LLM 不可解析的概率显著上升——本轮实测）。
"""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "test_agent.db"

SUPP = [("A1", 1), ("B1", 2), ("X6", 2), ("X7", 3)]      # (cell, 还需的已阅卷数)


def latest_task(cell: str) -> int:
    conn = sqlite3.connect(DB)
    tid = conn.execute(
        "SELECT MAX(t.id) FROM test_tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE p.name=?", (f"grid-{cell}-corpus-001",)).fetchone()[0]
    conn.close()
    return tid


def graded_count(cell: str) -> int:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT t.id FROM test_tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE p.name=?", (f"grid-{cell}-corpus-001",)).fetchall()
    conn.close()
    n = 0
    for (tid,) in rows:
        gf = ROOT / "data" / "corpus" / "corpus-001" / f"grade_task{tid}.json"
        if gf.exists() and "recall" in gf.read_text(encoding="utf-8"):
            n += 1
    return n


def main() -> int:
    for cell, need in SUPP:
        got = 0
        attempts = 0
        while got < need and attempts < need * 3:      # 退化重试上限：每格 3 倍
            attempts += 1
            time.sleep(20)
            subprocess.run([sys.executable, "scripts/run_grid.py", cell, "corpus-001"],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=1800)
            tid = latest_task(cell)
            g = subprocess.run([sys.executable, "scripts/grade_corpus.py", str(tid),
                                "corpus-001", f"{cell} 对照格 补充#{attempts}"],
                               capture_output=True, text=True, cwd=str(ROOT), timeout=600)
            ok = '"recall"' in g.stdout
            got += 1 if ok else 0
            try:
                obj, _ = json.JSONDecoder().raw_decode(g.stdout.strip())
                recall = obj.get("recall")
            except json.JSONDecodeError:
                recall = "?"
            print(f"[supp] {cell} #{attempts}: task={tid} "
                  f"{'graded recall=' + str(recall) if ok else 'degenerate→重试'}"
                  f"（本格已阅 {got}/{need}）", flush=True)
        print(f"[supp] {cell} 完成：graded={graded_count(cell)}", flush=True)
    print("[supp] 全部完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
