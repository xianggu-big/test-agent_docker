"""X-BREADTH 二次度量：条件总数/用例数/阅卷检出（只读）。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
OUT = ROOT / "data" / "reports" / "e9_full"


def main() -> int:
    conn = sqlite3.connect(r"file:d:/test agent_docker/data/test_agent.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = []
    for corpus in ("corpus-003", "corpus-001"):
        for group, pat in (("baseline", f"e9a1_{corpus}_00_n*.json"),
                           ("xbreadth", f"xb_{corpus}_n*.json")):
            for f in sorted(OUT.glob(pat)):
                rec = json.loads(f.read_text(encoding="utf-8"))
                if rec.get("status") != "done":
                    continue
                tid = rec["task_id"]
                n_cond = conn.execute(
                    "SELECT COUNT(*) FROM test_conditions WHERE task_id=?", (tid,)).fetchone()[0]
                prio = [dict(r) for r in conn.execute(
                    "SELECT priority, COUNT(*) n FROM test_conditions WHERE task_id=?"
                    " GROUP BY priority", (tid,))]
                n_cases = conn.execute(
                    """SELECT COUNT(*) FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
                       WHERE s.task_id=?""", (tid,)).fetchone()[0]
                # 阅卷检出（有档才看）
                g = ROOT / "data" / "corpus" / corpus / f"grade_task{tid}.json"
                detected = None
                if g.exists():
                    detected = json.loads(g.read_text(encoding="utf-8")).get("detected")
                rows.append({"corpus": corpus, "group": group, "cell": f.stem,
                             "conditions": n_cond, "by_priority": prio, "cases": n_cases,
                             "detected": detected, "tokens": rec.get("tokens_spent")})
    conn.close()
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
