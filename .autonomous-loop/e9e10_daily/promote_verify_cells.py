"""验证格留档：430(hybrid)/432(auto) 写正式格 JSON（432 的 L1 摘要从真库补录）。"""
import json
from pathlib import Path

OUT = Path(r"d:\test agent_docker\data\reports\e9_full")
LEGS_TAIL = {
    "L2": {"verdict": "skipped", "stop_reason": "无端点提取路径（no_spec）"},
    "L3": {"verdict": "stop", "stop_reason": "未找到 FastAPI 应用模块"},
    "L4": {"verdict": "skipped", "stop_reason": "无端点清单"},
}

rec430 = {
    "arm": "A2", "corpus": "corpus-001", "n": 1, "task_id": 430,
    "label": "e9a2-hybrid-corpus-001-n1", "at": "2026-11-06 12:38:38",
    "ai_reviewer": "hybrid", "tokens_spent": 25955, "status": "done",
    "legs": {"L1": {"verdict": "stop", "passed": 18, "failed": 3, "coverage_line": 0.96,
                    "round_no": 1,
                    "stop_reason": "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）"},
             **LEGS_TAIL},
    "verdict": "stop", "passed": 18, "failed": 3,
    "stop_reason": "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）",
    "review_decisions_summary": {"approve": 15, "reject": 2, "insert": 9},
    "note": "两阶段链验证格：D2 代审钩子真实参与（review_decisions 落库）",
}
rec432 = {
    "arm": "A2", "corpus": "corpus-001", "n": 1, "task_id": 432,
    "label": "e9a2-auto-corpus-001-n1", "at": "2026-11-06 12:38:44",
    "ai_reviewer": "auto", "tokens_spent": 103522, "status": "done",
    "legs": {"L1": {"verdict": None, "passed": 113, "failed": 10, "coverage_line": 0.98,
                    "round_no": 1, "task_status": "failed",
                    "note": "摘要补录自真库 test_reports（auto 返回值取字段 bug 期产出，"
                            "终态=证人如实上报家族；verdict 不推断留空）"},
             **LEGS_TAIL},
    "verdict": None, "passed": 113, "failed": 10, "stop_reason": None,
    "review_decisions_summary": {"approve": 123, "reject": 43, "insert": 14},
    "note": "两阶段链验证格：D2 多轮循环+增补真实参与（180 条决策），token 4 倍于 off/hybrid",
}
(OUT / "e9a2_hybrid_corpus-001_n1.json").write_text(
    json.dumps(rec430, ensure_ascii=False, indent=1), encoding="utf-8")
(OUT / "e9a2_auto_corpus-001_n1.json").write_text(
    json.dumps(rec432, ensure_ascii=False, indent=1), encoding="utf-8")
for p in OUT.glob("*.params"):
    p.unlink()
print("promoted 430/432; params cleaned")
