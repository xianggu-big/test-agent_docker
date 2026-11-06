"""X-BREADTH 对照判据计算（只读）：条件覆盖函数域数 + 检出对比。零写入零 LLM。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "reports" / "e9_full"


def fn_domains(task_id: int, corpus: str) -> set[str]:
    from test_agent.skills.gen_tests import source_outline
    outline = source_outline(ROOT / "data" / "corpus" / corpus / "project", "python")
    functions = (outline or {}).get("functions") or set()
    conn = sqlite3.connect(r"file:d:/test agent_docker/data/test_agent.db?mode=ro", uri=True)
    conds = [r[0] for r in conn.execute(
        "SELECT condition FROM test_conditions WHERE task_id=?", (task_id,))]
    conn.close()
    text = " ".join(conds)
    return {fn for fn in functions if fn in text}


def main() -> int:
    rows = []
    for corpus in ("corpus-003", "corpus-001"):
        for group, pat in (("baseline", f"e9a1_{corpus}_00_n*.json"),
                           ("xbreadth", f"xb_{corpus}_n*.json")):
            for f in sorted(OUT.glob(pat)):
                rec = json.loads(f.read_text(encoding="utf-8"))
                if rec.get("status") != "done":
                    continue
                doms = fn_domains(rec["task_id"], corpus)
                l1 = (rec.get("legs") or {}).get("L1") or {}
                rows.append({"group": group, "corpus": corpus, "cell": f.stem,
                             "task_id": rec["task_id"], "domains": len(doms),
                             "sorted_domains": ",".join(sorted(doms)[:6]),
                             "verdict": rec.get("verdict"),
                             "failed": l1.get("failed", rec.get("failed")),
                             "passed": l1.get("passed", rec.get("passed"))})
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    # 组均值
    import statistics
    for corpus in ("corpus-003", "corpus-001"):
        for group in ("baseline", "xbreadth"):
            doms = [r["domains"] for r in rows if r["corpus"] == corpus and r["group"] == group]
            fails = [r["failed"] or 0 for r in rows
                     if r["corpus"] == corpus and r["group"] == group]
            if doms:
                print(f"{corpus}/{group}: 域数均值={statistics.mean(doms):.2f} "
                      f"(n={len(doms)}, 明细={doms}) 证人均值={statistics.mean(fails):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
