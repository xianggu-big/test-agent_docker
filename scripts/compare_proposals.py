"""三方案对比聚合（docs/设计方案对比实验.md 的数据出口）。

用法：python scripts/compare_proposals.py
从 DB（grid-* 任务）+ 阅卷明细（grade_task*.json）+ 变异明细（mutation_task*.json）汇总：
每格 recall / detected / extra_suspect / 用例数 / 弱断言率（回算，基线格可比）/ RTM 条件覆盖 /
变异杀死率。独有指标对基线同样回算，保证横向可比。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.gen_tests import assertion_audit  # noqa: E402


def weak_rate_of(contents: list[str]) -> float | None:
    audits = [assertion_audit(c) for c in contents]
    if not audits:
        return None
    return round(sum(1 for a in audits if a["strong"] == 0) / len(audits), 3)


def load_task(conn, tid: int) -> dict:
    rep = conn.execute("SELECT * FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    rtm = conn.execute("SELECT source, matrix FROM rtm_maps WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    cases = conn.execute("""SELECT c.name, c.content FROM test_cases c
                            JOIN test_scripts s ON s.id=c.script_id
                            WHERE s.task_id=? AND c.enabled=1 AND c.source='ai'""", (tid,)).fetchall()
    d = {"task_id": tid}
    if rep:
        d.update(verdict=(json.loads(rep["defects"]) or {}).get("failure_kind") or "pass",
                 passed=rep["passed"], failed=rep["failed"], coverage=rep["coverage_line"])
    if rtm:
        m = json.loads(rtm["matrix"])
        d["rtm"] = f"{sum(1 for x in m if x.get('status') == 'covered')}/{len(m)}({rtm['source'][:4]})"
    d["cases"] = len(cases)
    d["weak_rate"] = weak_rate_of([r["content"] for r in cases])
    d["pbt"] = sum(1 for r in cases
                   if "@given" in (r["content"] or "") or "属性测试" in (r["name"] or ""))
    return d


def load_grade(corpus: str, tid: int) -> dict:
    p = ROOT / "data" / "corpus" / corpus / f"grade_task{tid}.json"
    if not p.exists():
        return {}
    g = json.loads(p.read_text(encoding="utf-8"))
    return {"recall": g.get("recall"), "detected": g.get("detected"),
            "uncertain": g.get("uncertain"), "extra": g.get("extra_suspect")}


def load_mutation(corpus: str, tid: int) -> dict:
    p = ROOT / "data" / "corpus" / corpus / f"mutation_task{tid}.json"
    if not p.exists():
        return {}
    m = json.loads(p.read_text(encoding="utf-8"))
    return {"kill": m.get("kill_rate"), "mutants": m.get("mutants_total")}


def main() -> int:
    conn = sqlite3.connect(ROOT / "data" / "test_agent.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT t.id, p.name FROM test_tasks t JOIN projects p ON p.id=t.project_id
                           WHERE p.name LIKE 'grid-%' ORDER BY t.id""").fetchall()
    cells: dict[str, dict[str, list[int]]] = {}
    for r in rows:
        m = re.match(r"grid-(\w+)-(corpus-\d+)", r["name"])
        if m:
            cells.setdefault(m.group(2), {}).setdefault(m.group(1), []).append(r["id"])

    for corpus in sorted(cells):
        print(f"\n===== {corpus} =====")
        header = (f"{'格':6} {'任务':14} {'recall':7} {'det+unc':8} {'extra':6} "
                  f"{'用例':5} {'弱断率':7} {'RTM覆盖':14} {'杀死率':8} {'PBT条':6}")
        print(header)
        for cell in sorted(cells[corpus]):
            tids = cells[corpus][cell]
            recalls, kills, weaks, covs = [], [], [], []
            for tid in tids:
                d = load_task(conn, tid)
                d.update(load_grade(corpus, tid))
                d.update(load_mutation(corpus, tid))
                recalls.append(d.get("recall"))
                weaks.append(d.get("weak_rate"))
                if d.get("kill") is not None:
                    kills.append(d["kill"])
                print(f"{cell:6} {str(tid):14} "
                      f"{str(d.get('recall')):7} "
                      f"{str(d.get('detected')) + '+' + str(d.get('uncertain')):8} "
                      f"{str(d.get('extra')):6} "
                      f"{str(d.get('cases')):5} "
                      f"{str(d.get('weak_rate')):7} "
                      f"{str(d.get('rtm')):14} "
                      f"{str(d.get('kill')):8} "
                      f"{str(d.get('pbt')):6}")
            valid_recall = [r for r in recalls if r is not None]
            if valid_recall:
                mean = sum(valid_recall) / len(valid_recall)
                print(f"  → {cell} recall 均值 {mean:.2f} (n={len(valid_recall)})"
                      + (f"，杀死率均值 {sum(kills)/len(kills):.2f}" if kills else ""))
    print("\n注：corpus-002 基线=任务46（早于 grid 命名，recall=0.0，见 grade_task46.json）；"
          "X1 格=全默认对照组。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
