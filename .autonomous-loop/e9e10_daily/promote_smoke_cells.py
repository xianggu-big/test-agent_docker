"""小样格留档：423(A1 c01)/424(A2 off) 数据有效直接占格；425(hybrid) 钩子未触发=off 等价，
写侧档留痕不占格（正式波次重跑），tokens 按 task_id 真值修正。"""
import json
from pathlib import Path

OUT = Path(r"d:\test agent_docker\data\reports\e9_full")
REAL_TOKENS = {423: 22724, 424: 24492, 425: 22420}

cells = [
    ("e9a1_corpus-001_01_n1", 423, "done"),
    ("e9a2_off_corpus-001_n1", 424, "done"),
]
legs424 = {
    "L1": {"verdict": "stop", "stop_reason": "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）",
           "passed": 60, "failed": 2, "coverage_line": 1.0, "round_no": 1},
    "L2": {"verdict": "skipped", "stop_reason": "无端点提取路径（no_spec）"},
    "L3": {"verdict": "stop", "stop_reason": "未找到 FastAPI 应用模块"},
    "L4": {"verdict": "skipped", "stop_reason": "无端点清单"},
}
recs = {
    "e9a1_corpus-001_01_n1": {
        "arm": "A1", "corpus": "corpus-001", "n": 1, "task_id": 423,
        "label": "e9a1-c01-corpus-001-n1", "at": "2026-11-06 12:34:30", "combo_idx": 1,
        "flags": {"GEN_METHODS_ENABLED": False, "PLAN_ENABLED": True,
                  "COVERAGE_FEEDBACK_ENABLED": True, "GEN_BOUNDARY_ATTACK": False,
                  "WITNESS_CONTINUE_ENABLED": False, "GEN_CROSS_REVIEW_ENABLED": False},
        "legs": {"L1": {"verdict": "stop", "round_no": 1, "passed": 41, "failed": 3,
                        "coverage_line": 1.0,
                        "stop_reason": "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）"}},
        "verdict": "stop", "status": "done"},
    "e9a2_off_corpus-001_n1": {
        "arm": "A2", "corpus": "corpus-001", "n": 1, "task_id": 424,
        "label": "e9a2-off-corpus-001-n1", "at": "2026-11-06 12:34:30", "ai_reviewer": "off",
        "legs": legs424, "verdict": "stop", "status": "done",
        "stop_reason": legs424["L1"]["stop_reason"], "passed": 60, "failed": 2},
}
for cid, tid, _st in cells:
    rec = recs[cid]
    rec["tokens_spent"] = REAL_TOKENS[tid]
    rec["note"] = "smoke cell promoted（tokens_spent 按 task_id 真值修正，v2 并发记账 bug 期产出）"
    (OUT / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                     encoding="utf-8")

side = {"task_id": 425, "arm": "A2", "ai_reviewer": "hybrid", "corpus": "corpus-001", "n": 1,
        "tokens_spent": REAL_TOKENS[425], "at": "2026-11-06 12:34:30",
        "status": "error", "error": "reviewer hook not triggered（v2 修钩子前的 smoke run："
        "run_task 单阶段路径无代审钩子，数据=off 等价；不占正式格，该格由正式波次重跑）",
        "legs": {"L1": {"verdict": "stop", "passed": 34, "failed": 4, "coverage_line": 0.99}}}
(OUT / "smoke_task425_hybrid_ineffective.json").write_text(
    json.dumps(side, ensure_ascii=False, indent=1), encoding="utf-8")

for p in OUT.glob("*.params"):
    p.unlink()
print("promoted:", [c[0] for c in cells], "+ side record for 425")
