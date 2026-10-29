"""D8 存量实验队列驱动（断点续跑）：A1/B1/C1 × 双语料 + X6/X7 各补到 n>=3，跑完即阅卷。

用法：python scripts/run_d8_queue.py [目标n]   （默认 3；已有任务数计入 n，只补差额）
每格顺序：run_grid（端到端真机）→ DB 查本格最新任务号 → grade_corpus（LLM 仲裁）→
结果 JSONL 追加 data/reports/d8_queue_results.jsonl（幂等重跑安全）。
"""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "test_agent.db"
OUT = ROOT / "data" / "reports" / "d8_queue_results.jsonl"

QUEUE = []                                  # (cell, corpus, label)
for corpus in ("corpus-001", "corpus-002"):
    for cell in ("A1", "B1", "C1"):
        QUEUE.append((cell, corpus, f"{cell} 对照格 n>=3"))
for cell in ("X6", "X7"):
    QUEUE.append((cell, "corpus-001", f"{cell} 消融格"))


def existing_count(cell: str, corpus: str) -> int:
    conn = sqlite3.connect(DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM test_tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE p.name=?", (f"grid-{cell}-{corpus}",)).fetchone()[0]
    conn.close()
    return n


def latest_task(cell: str, corpus: str) -> int:
    conn = sqlite3.connect(DB)
    tid = conn.execute(
        "SELECT MAX(t.id) FROM test_tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE p.name=?", (f"grid-{cell}-{corpus}",)).fetchone()[0]
    conn.close()
    return tid


def main() -> int:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for cell, corpus, label in QUEUE:
        have = existing_count(cell, corpus)
        need = target - have
        print(f"[queue] {cell}/{corpus}: 已有 {have}，目标 {target}，补 {max(0, need)}", flush=True)
        for i in range(max(0, need)):
            before = existing_count(cell, corpus)
            r = subprocess.run([sys.executable, "scripts/run_grid.py", cell, corpus],
                               capture_output=True, text=True, cwd=str(ROOT), timeout=1800)
            print(r.stdout[-500:], r.stderr[-300:] if r.returncode else "", flush=True)
            if existing_count(cell, corpus) > before:      # 新任务落库成功才阅卷
                tid = latest_task(cell, corpus)
                g = subprocess.run([sys.executable, "scripts/grade_corpus.py", str(tid),
                                    corpus, f"{label} #{before + 1}"], capture_output=True,
                                   text=True, cwd=str(ROOT), timeout=600)
                line = (g.stdout.strip().splitlines() or ["{}"])[0]
                try:
                    metrics = json.loads(line)
                except json.JSONDecodeError:
                    metrics = {"raw": line[:200]}
                with open(OUT, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"cell": cell, "corpus": corpus, "task_id": tid,
                                        **{k: metrics.get(k) for k in
                                           ("recall", "detected", "uncertain", "extra_suspect")}},
                                       ensure_ascii=False) + "\n")
                print(f"[queue] {cell}/{corpus} task={tid} 阅卷: recall={metrics.get('recall')}",
                      flush=True)
    print("[queue] 全队列完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
