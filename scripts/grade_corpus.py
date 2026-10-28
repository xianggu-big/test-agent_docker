"""盲测语料阅卷 —— 答题人的缺陷报告 ↔ 出题人 answer key 对齐（三方盲测协议第三角色）。

用法：python scripts/grade_corpus.py <task_id> <corpus_id> [--dry]
阅卷对象 = 该任务证人用例（最新一轮 run 中断言失败且被冻结的用例，即"疑似缺陷报告"单元）。
证人名单来源：宿主本地重放工作区（run_task 每次 clone 重建 data/workspaces/task_<id>，
该目录即最后一轮的确切文件集；语料为纯标准库项目，宿主重放与容器执行语义等价）。
输出 = 每条 planted_defect 的 detected/not/uncertain + extra_suspect 清单 + 指标；
明细落 data/corpus/<corpus_id>/grade_task<task_id>.json，摘要追加 docs/链路方法测试文档.md §5。

诚实边界：对齐仲裁用 LLM（DeepSeek），结果需人工抽查（spot_check.py 抽 20%）；
证人用例可能只是"撞上缺陷的输入"，LLM 负责判断其断言意图与哪个植入缺陷对应；
工作区缺失无法重放时退化为"全量用例当证人"并显式声明不可靠（旧版即此模式，extra_suspect 虚高）。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402
from test_agent.graph.repair import failing_nodeids  # noqa: E402
from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "链路方法测试文档.md"


_AGENT_FILE = re.compile(r"((?:test_gen_r\d+|test_imported)\.py::.+)$")


def _replay_failed_nodeids(task_id: int) -> list[str] | None:
    """宿主重放最后一轮工作区，返回 FAILED nodeid 列表（归一化为 <文件>::<用例名>，
    剥掉 pytest 以路径运行时带的目录前缀）；工作区缺失返回 None。"""
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        return None
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(ws), "-q", "--tb=no", "--no-header", "-rf"],
        capture_output=True, text=True, timeout=300)
    out = []
    for nid in failing_nodeids(r.stdout):
        m = _AGENT_FILE.search(nid)
        if m:
            out.append(m.group(1))
    return out


def witnesses_of(task_id: int) -> tuple[list[dict], str]:
    """证人 = 最新一轮 run 中断言失败的用例。返回 (用例列表, 证人来源说明)。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.name, c.content, s.version, s.framework FROM test_cases c
           JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.name LIKE 'test_%' ORDER BY c.id DESC""",
        (task_id,)).fetchall()
    failed = _replay_failed_nodeids(task_id)
    if failed is None:
        note = "工作区缺失无法重放，证人集退化为全量用例（extra_suspect 必然虚高，指标仅供参考）"
        sel = rows
    else:
        failed_set = set(failed)
        sel = []
        for r in rows:
            fname = ("test_imported.py" if r["framework"] == "imported"
                     else f"test_gen_r{r['version']}.py")
            if f"{fname}::{r['name']}" in failed_set:
                sel.append(r)
        note = f"宿主重放工作区：{len(failed_set)} 个失败 nodeid，命中证人 {len(sel)} 条"
    out = [{"name": r["name"], "content": r["content"][:400]} for r in sel]
    return out, note


def grade(task_id: int, corpus_id: str, dry: bool = False,
          label: str = "默认组 V2-V4=True, V1=prune") -> dict:
    gt_path = ROOT / "data" / "corpus" / corpus_id / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    planted = [d for d in gt["planted_defects"] if d.get("detectable_by") == "assertion"]
    expected_miss = [d for d in gt["planted_defects"] if d.get("detectable_by") != "assertion"]
    witnesses, witness_note = witnesses_of(task_id)
    if not witnesses:
        err = {"error": "该任务无证人用例（可能全过/未跑完/重放名单与用例库无交集），无法阅卷",
               "witness_note": witness_note}
        print(json.dumps(err, ensure_ascii=False))
        return err

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        err = {"error": "未配置 DEEPSEEK_API_KEY，仲裁不可用"}
        print(json.dumps(err, ensure_ascii=False))
        return err
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
        "witness_note": witness_note,
        "detail": verdict,
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    if not dry:
        # 明细落盘（20% 人工抽查与论文复现的数据源）
        detail_path = ROOT / "data" / "corpus" / corpus_id / f"grade_task{task_id}.json"
        detail_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
        row = (f"| 盲测 | {corpus_id} | {label} | "
               f"{metrics['detected']}/{metrics['planted_assertion']} (recall={metrics['recall']}) | "
               f"{metrics['extra_suspect']}条待人工复核/{metrics['uncertain']}条uncertain | "
               f"见任务{task_id} | — | — | — | 2026-10-28 |")
        with open(DOC, "a", encoding="utf-8") as f:
            f.write(row + "\n")
        print(f"\n明细已落 {detail_path.name}，摘要已追加到 {DOC.name} §5")
    return metrics


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    grade(int(sys.argv[1]), sys.argv[2], dry="--dry" in sys.argv,
          label=sys.argv[3] if len(sys.argv) > 3 else "默认组 V2-V4=True, V1=prune")
