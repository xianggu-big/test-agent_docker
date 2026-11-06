"""k=2 补样格留档（覆盖 abandoned 档）+ 清 params。"""
import json
from pathlib import Path

OUT = Path(r"d:\test agent_docker\data\reports\e9_full")
rec = {
    "arm": "A1", "corpus": "corpus-001", "n": 2, "task_id": 586,
    "label": "e9a1-c00-corpus-001-n2", "at": "2026-11-06 14:07:11", "combo_idx": 0,
    "flags": {"GEN_METHODS_ENABLED": True, "PLAN_ENABLED": True,
              "COVERAGE_FEEDBACK_ENABLED": True, "GEN_BOUNDARY_ATTACK": False,
              "WITNESS_CONTINUE_ENABLED": False, "GEN_CROSS_REVIEW_ENABLED": False},
    "legs": {"L1": {"verdict": "stop", "passed": 34, "failed": 4, "coverage_line": 0.99,
                    "round_no": 1,
                    "stop_reason": "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）"}},
    "verdict": "stop", "passed": 34, "failed": 4, "coverage": 0.99, "status": "done",
    "tokens_spent": 18428,
    "note": "K 曲线 k=2 补样（v1 中断格 abandoned 后重跑，task422 作废留痕）",
}
(OUT / "e9a1_corpus-001_00_n2.json").write_text(
    json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
for p in OUT.glob("*.params"):
    p.unlink()
print("k=2 cell promoted")
