"""X 格 n>=3 聚合 —— 读 grade_task*.json 按格统计 recall 均值/标准差，输出 md 表格行。

用法：python scripts/aggregate_grid.py
数据来源：data/corpus/<corpus>/grade_task<tid>.json（grade_corpus.py 落盘）
格标签来源：projects.name LIKE 'grid-<cell>-<corpus>'（run_grid.py 写入）
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, p.name FROM test_tasks t JOIN projects p ON p.id=t.project_id
           WHERE p.name LIKE 'grid-%' ORDER BY t.id""").fetchall()
    cells: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        _, cell, corpus = r["name"].split("-", 2)   # grid-<cell>-<corpus>
        gf = ROOT / "data" / "corpus" / corpus / f"grade_task{r['id']}.json"
        if not gf.exists():
            continue
        g = json.loads(gf.read_text(encoding="utf-8"))
        if "recall" not in g:
            continue
        cells.setdefault((corpus, cell), []).append(
            {"tid": r["id"], "recall": g["recall"], "extra": g.get("extra_suspect", 0),
             "planted": g.get("planted_assertion"), "detected": g.get("detected")})

    print("| 语料 | 格 | n | recall 均值±标准差 | 检出明细 | extra 均值 |")
    print("|---|---|---|---|---|---|")
    for (corpus, cell), items in sorted(cells.items()):
        recalls = [x["recall"] for x in items if x["recall"] is not None]
        if not recalls:
            continue
        mean = statistics.mean(recalls)
        std = statistics.stdev(recalls) if len(recalls) > 1 else 0.0
        detail = "/".join(f"{x['detected'] or 0}" for x in items)
        extra = statistics.mean([x["extra"] for x in items])
        print(f"| {corpus} | {cell} | {len(items)} | {mean:.2f}±{std:.2f} | "
              f"{detail}（分母 {items[0]['planted']}） | {extra:.1f} |")


if __name__ == "__main__":
    main()
