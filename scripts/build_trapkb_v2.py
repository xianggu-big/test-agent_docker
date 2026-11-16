# -*- coding: utf-8 -*-
"""失败语料聚类器 → 动态陷阱库 v2 落库（E10 Wave 1 前置，零 API 成本）。

弹药：E9 failed 任务的失败用例明细（test_case_results，35,563 行）。
方法：defect_signature 同根因聚类（复用 defect_handoff 机制）→ 按组规模排序 →
top 簇写入 data/reports/e10/trapkb_v2.json（结构对齐 prompt_library.PITFALL_KB：
{类别: [{pattern, message, source_task}]}），供 gen_tests build_user_prompt 注入。

本脚本只产 JSON 留档 + 只读生产库（红线），注入由 E10 驱动器经 flags 通道做。
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

from test_agent.skills.defect_handoff import defect_signature

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = ROOT / "data" / "test_agent.db"
OUT = ROOT / "data" / "reports" / "e10" / "trapkb_v2.json"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    # E9 失败用例明细（含消息）——只取 AI 生成用例的失败（origin=generated 是反哺对象）
    rows = c.execute("""
        SELECT tcr.nodeid, tcr.name, tcr.message, t.id AS task_id, p.name AS proj
        FROM test_case_results tcr
        JOIN test_tasks t ON t.id = tcr.task_id
        JOIN projects p ON p.id = t.project_id
        WHERE p.name LIKE 'e9a%' AND tcr.status IN ('failed', 'error')
          AND tcr.origin = 'generated'
        LIMIT 60000""").fetchall()
    c.close()
    print(f"语料行: {len(rows)}（origin=generated failed/error）")

    # 聚类：签名 → (任务, 消息, 用例名) 列表
    buckets: dict[str, list] = {}
    for r in rows:
        d = {"message": r["message"] or "", "name": r["name"] or "",
             "nodeid": r["nodeid"] or ""}
        sig = defect_signature(d, failure_kind="generated_failure")
        buckets.setdefault(sig, []).append({
            "task": r["task_id"], "proj": r["proj"],
            "name": (r["name"] or "")[:60],
            "msg": (r["message"] or "")[:200],
        })

    # top 簇：规模 ≥5 才入陷阱库（小簇=个案噪声）
    sized = [(sig, members) for sig, members in buckets.items() if len(members) >= 5]
    sized.sort(key=lambda x: -len(x[1]))
    trapkb = []
    for sig, members in sized[:40]:
        msgs = Counter(m["msg"][:80] for m in members)
        trapkb.append({
            "signature": sig,
            "size": len(members),
            "sample_tasks": sorted({m["task"] for m in members})[:8],
            "sample_proj": sorted({m["proj"] for m in members})[:4],
            "top_messages": [m for m, _ in msgs.most_common(3)],
        })

    OUT.write_text(json.dumps({
        "generated_at": __import__("time").strftime("%F %T"),
        "source_rows": len(rows), "clusters_total": len(buckets),
        "clusters_ge5": len(sized), "traps": trapkb,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"聚类: {len(buckets)} 簇 → ≥5 规模 {len(sized)} 簇 → 入库 top {len(trapkb)}")
    for t in trapkb[:8]:
        print(f"  [{t['size']:>4}] {t['signature']} {t['top_messages'][0][:60]}")
    print("落盘:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
