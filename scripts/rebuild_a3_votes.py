# -*- coding: utf-8 -*-
"""A3 K 曲线 → 条件级投票矩阵重建（E10 Arm 4/5 数据源，零 LLM）。

弹药：E9-A3 留档 54 格 = K=4..30 × 2 语料（每 K 档 2 格）——每格是同任务的一次
独立采样（self-consistency 样本）。本脚本把 54 格的执行结果按 (语料, 用例名) 对齐
→ 条件级 verdict 矩阵：
  - 行=条件（用例名规范化后），列=K 采样序
  - 单元格=该采样下此用例 pass/fail
产出（data/reports/e10/a3_vote_matrix.json）：
  - per_condition: 每条件的 pass_rate（跨 K 采样）、分歧度（pass_rate ∈ (0,1)）、
    verdict 序列 —— Arm 5 分歧精炼选 top 分歧条件；Arm 4 对抗验证的 oracle 清单
  - K 一致性曲线：majority@K / union@K 随 K 的变化（投票价值在并集 E9 结论的复核）
只读生产库 + 留档 JSON。
"""
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = ROOT / "data" / "test_agent.db"
OUT = ROOT / "data" / "reports" / "e10" / "a3_vote_matrix.json"


def _norm(name: str) -> str:
    """用例名规范化（同 normalize_case_name 口径）：剥 test_/TestR\d_ 前缀小写比对。"""
    n = (name or "").strip()
    n = re.sub(r"^test_", "", n, flags=re.I)
    n = re.sub(r"^Test(?:R\d+|Fb\d+)?_", "", n)
    n = re.sub(r"^Test", "", n)
    return n.lower()


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    # A3 格：项目名 e9a3-<corpus>-kNN → (corpus, k)
    rows = c.execute("""
        SELECT p.name AS proj, p.primary_language AS lang, t.id AS tid,
               tcr.name, tcr.status
        FROM test_case_results tcr
        JOIN test_tasks t ON t.id = tcr.task_id
        JOIN projects p ON p.id = t.project_id
        WHERE p.name LIKE 'e9a3%' AND tcr.status IN ('passed', 'failed', 'error')
        ORDER BY p.name, tcr.id""").fetchall()
    c.close()
    print(f"A3 逐用例行: {len(rows)}")

    # proj=e9a3-corpus-001-k04 → corpus=corpus-001, k=4；采样序=同 k 档内的格序（2 格）
    cells: dict[tuple, dict] = {}     # (corpus, k, sample_idx) → {norm_name: status}
    grid: dict[tuple, dict] = defaultdict(dict)   # (corpus, k) → {sample: {norm: status}}
    for r in rows:
        m = re.match(r"e9a3-(corpus-\d+)-k(\d+)", r["proj"] or "")
        if not m:
            continue
        corpus, k = m.group(1), int(m.group(2))
        key = (corpus, k)
        idx = len(grid[key])
        cells.setdefault((corpus, k), {})[_norm(r["name"])] = (
            1 if r["status"] == "passed" else 0)
        grid[key][idx] = cells[(corpus, k)]

    # 条件级矩阵（按语料）：跨 K 档对齐同一条件
    per_corpus: dict[str, dict] = {}
    for corpus in {c2 for c2, _ in grid}:
        samples: list[dict] = []
        for (c2, k), sample_map in sorted(grid.items(), key=lambda x: x[0][1]):
            if c2 != corpus:
                continue
            for idx, votes in sample_map.items():
                samples.append(votes)
        conds: set = set()
        for v in samples:
            conds.update(v)
        per_cond = {}
        for cond in sorted(conds):
            votes = [v[cond] for v in samples if cond in v]
            if not votes:
                continue
            per_cond[cond] = {
                "n_samples": len(votes), "pass_rate": round(sum(votes) / len(votes), 3),
                "divergent": int(0 < sum(votes) < len(votes)),
                "verdicts": votes,
            }
        # K 一致性曲线（单采样序列近似：按格逐个累积投票）
        union_at = {}
        majority_at = {}
        acc = defaultdict(int)
        for i, v in enumerate(samples, 1):
            for cond, val in v.items():
                acc[cond] += val
            total_pos = sum(1 for cond in acc if acc[cond] > 0)
            union_at[i] = round(total_pos / max(1, len(acc)), 3)
        per_corpus[corpus] = {
            "n_samples": len(samples), "n_conditions": len(per_cond),
            "per_condition": per_cond,
            "union_coverage_at": union_at,
            "n_divergent": sum(1 for x in per_cond.values() if x["divergent"]),
        }

    OUT.write_text(json.dumps({"built_at": __import__("time").strftime("%F %T"),
                               "per_corpus": per_corpus},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    for corpus, d in per_corpus.items():
        print(f"{corpus}: 样本 {d['n_samples']} 条件 {d['n_conditions']} "
              f"分歧条件 {d['n_divergent']} union@1={d['union_coverage_at'].get(1)} "
              f"union@末={list(d['union_coverage_at'].values())[-1] if d['union_coverage_at'] else '—'}")
    print("落盘:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
