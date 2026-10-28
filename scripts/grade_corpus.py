"""盲测语料阅卷 —— 答题人的缺陷报告 ↔ 出题人 answer key 对齐（三方盲测协议第三角色）。

用法：python scripts/grade_corpus.py <task_id> <corpus_id> [--dry]
阅卷对象 = 该任务证人用例（product_defect 冻结的断言失败用例，即"疑似缺陷报告"单元）。
输出 = 每条 planted_defect 的 detected/not/uncertain + extra_suspect 清单 + 指标，
并追加写入 docs/链路方法测试文档.md §5 实验记录表（--dry 只打印不落盘）。

诚实边界：对齐仲裁用 LLM（DeepSeek），结果需人工抽查（文档 §6.5）；
证人用例可能只是"撞上缺陷的输入"，LLM 负责判断其断言意图与哪个植入缺陷对应。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "链路方法测试文档.md"


def witnesses_of(task_id: int) -> list[dict]:
    """证人 = 最新报告轮里断言失败且被判 product_defect 的用例（按 name 对齐）。"""
    conn = get_conn()
    failed_names = conn.execute(
        """SELECT c.name, c.content, c.method FROM test_cases c
           JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.name LIKE 'test_%' ORDER BY c.id DESC""",
        (task_id,)).fetchall()
    out = []
    for r in failed_names:
        out.append({"name": r["name"], "content": r["content"][:400]})
    return out


def grade(task_id: int, corpus_id: str, dry: bool = False) -> dict:
    gt_path = ROOT / "data" / "corpus" / corpus_id / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    planted = [d for d in gt["planted_defects"] if d.get("detectable_by") == "assertion"]
    expected_miss = [d for d in gt["planted_defects"] if d.get("detectable_by") != "assertion"]
    witnesses = witnesses_of(task_id)
    if not witnesses:
        return {"error": "该任务无证人用例（可能全过或未跑完），无法阅卷"}

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {"error": "未配置 DEEPSEEK_API_KEY，仲裁不可用"}
    from test_agent.skills.repair_strategy import make_deepseek_fn
    llm = make_deepseek_fn(api_key)

    prompt = f"""## 阅卷任务
出题人植入的缺陷清单（答案键）：
{json.dumps(planted, ensure_ascii=False, indent=1)}

答题人（被测系统）报告的疑似缺陷（证人用例，断言失败即"预期行为与实际不符"）：
{json.dumps(witnesses, ensure_ascii=False, indent=1)}

## 任务
逐条判断答案键中每个缺陷是否被某条证人用例覆盖（该用例的断言意图/输入是否恰好落在该缺陷上）：
- detected：存在对应用例（给出用例名）
- uncertain：沾边但证据不足（说明原因）
- not：没有任何用例触及
另外列出"证人里有、答案键外"的用例（extra_suspect：可能是出题人没想到的真缺陷）。
只输出 JSON：{{"matches": [{{"id": "D1", "verdict": "detected|uncertain|not", "by": "用例名或空", "reason": "一句话"}}],
"extra_suspect": [{{"case": "用例名", "reason": "一句话"}}]}}"""
    raw = llm(prompt)
    m = None
    import re
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"error": "仲裁 LLM 输出不可解析", "raw": raw[-300:]}
    verdict = json.loads(m.group(0))

    detected = [x for x in verdict["matches"] if x["verdict"] == "detected"]
    uncertain = [x for x in verdict["matches"] if x["verdict"] == "uncertain"]
    metrics = {
        "task_id": task_id, "corpus_id": corpus_id,
        "planted_assertion": len(planted), "detected": len(detected),
        "uncertain": len(uncertain), "not": len(verdict["matches"]) - len(detected) - len(uncertain),
        "recall": round(len(detected) / len(planted), 2) if planted else None,
        "extra_suspect": len(verdict.get("extra_suspect", [])),
        "expected_miss_performance": len(expected_miss),
        "detail": verdict,
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    if not dry:
        row = (f"| 盲测 | {corpus_id} | 默认组 V2-V4=True, V1=prune | "
               f"{metrics['detected']}/{metrics['planted_assertion']} (recall={metrics['recall']}) | "
               f"{metrics['extra_suspect']}条待人工复核/{metrics['uncertain']}条uncertain | "
               f"见任务{task_id} | — | — | — | 2026-10-28 |")
        with open(DOC, "a", encoding="utf-8") as f:
            f.write(row + "\n")
        print(f"\n已追加到 {DOC.name} §5（人工复核后再整理为正式行）")
    return metrics


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    grade(int(sys.argv[1]), sys.argv[2], dry="--dry" in sys.argv)
