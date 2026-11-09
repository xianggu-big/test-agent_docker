"""E9/E10 每日续跑：状态盘点（只读）。"""
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")

for d in ("e9_full", "e10_full"):
    p = ROOT / "data" / "reports" / d
    c = Counter()
    tok = 0
    bad = []
    skip = {"k_curve.json", "arm1_corpus.json", "arm4_oracles.json"}
    for f in sorted(p.glob("*.json")):
        if f.name in skip:
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append(f.name + ":" + str(e)[:40])
            continue
        c[r.get("status", "?")] += 1
        tok += int(r.get("tokens_spent") or 0)
    print(d, dict(c), "tokens_spent_sum=", tok, "bad=", bad)

conn = sqlite3.connect(str(ROOT / "data" / "test_agent.db"))
conn.row_factory = sqlite3.Row
t = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok FROM llm_calls").fetchone()
print("llm_calls:", t["n"], "calls /", t["tok"], "tokens")
q = conn.execute("SELECT status, COUNT(*) n FROM exec_queue GROUP BY status").fetchall()
print("exec_queue:", {r["status"]: r["n"] for r in q})
conn.close()
