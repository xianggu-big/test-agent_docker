"""X 格 n>=3 聚合 —— 读 grade_task*.json 按格统计 recall 均值/标准差 + LLM token 成本，输出 md 表格行。

用法：python scripts/aggregate_grid.py
数据来源：data/corpus/<corpus>/grade_task<tid>.json（grade_corpus.py 落盘）+ llm_calls 台账（token 成本）
格标签来源：projects.name LIKE 'grid-<cell>-<corpus>'（run_grid.py 写入）
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load_token_costs(conn):
    """llm_calls 按任务聚合 → {task_id: (调用数, total_tokens)}；无行任务缺席（老任务无台账语义）。"""
    return {r["task_id"]: (r["calls"], r["tokens"]) for r in conn.execute(
        "SELECT task_id, COUNT(*) calls, COALESCE(SUM(total_tokens), 0) tokens "
        "FROM llm_calls GROUP BY task_id")}


def format_token_cell(items):
    """格内 token 均值；有台账的任务数 < n 时如实标注（缺席任务不计入均值）。"""
    toks = [x["tokens"] for x in items if x["tokens"] is not None]
    if not toks:
        return "—（无台账）"
    mean = statistics.mean(toks)
    return f"{mean:.0f}" if len(toks) == len(items) else f"{mean:.0f}（{len(toks)}/{len(items)}）"


def main() -> None:
    conn = get_conn()
    costs = load_token_costs(conn)
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
        usage = costs.get(r["id"])
        cells.setdefault((corpus, cell), []).append(
            {"tid": r["id"], "recall": g["recall"], "extra": g.get("extra_suspect", 0),
             "planted": g.get("planted_assertion"), "detected": g.get("detected"),
             "tokens": usage[1] if usage else None})

    print("| 语料 | 格 | n | recall 均值±标准差 | 检出明细 | extra 均值 | token 均值 |")
    print("|---|---|---|---|---|---|---|")
    for (corpus, cell), items in sorted(cells.items()):
        recalls = [x["recall"] for x in items if x["recall"] is not None]
        if not recalls:
            continue
        mean = statistics.mean(recalls)
        std = statistics.stdev(recalls) if len(recalls) > 1 else 0.0
        detail = "/".join(f"{x['detected'] or 0}" for x in items)
        extra = statistics.mean([x["extra"] for x in items])
        print(f"| {corpus} | {cell} | {len(items)} | {mean:.2f}±{std:.2f} | "
              f"{detail}（分母 {items[0]['planted']}） | {extra:.1f} | {format_token_cell(items)} |")


if __name__ == "__main__":
    main()
