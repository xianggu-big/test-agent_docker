"""阅卷仲裁人工抽查（docs/链路方法测试文档.md §6.5）：从阅卷明细抽 20% 生成复核清单。

用法：python scripts/spot_check.py [--rate 0.2]
扫描 data/corpus/*/grade_task*.json（grade_corpus.py 的落盘明细），对每份的
matches + extra_suspect 合并池按固定种子抽 20%，输出 markdown 复核清单到 stdout——
人工逐条填"同意/改判"后回填文档 §5 与论文附录（抽查记录本身即论文可信度证据）。
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261028   # 固定种子：抽查可复现（论文可写"种子抽样"）


def main() -> int:
    rate = 0.2
    if "--rate" in sys.argv:
        rate = float(sys.argv[sys.argv.index("--rate") + 1])
    pool = []
    for f in sorted((ROOT / "data" / "corpus").glob("corpus-*/grade_task*.json")):
        g = json.loads(f.read_text(encoding="utf-8"))
        pool += [{"corpus": g["corpus_id"], "task": g["task_id"], "kind": "对齐", **m}
                 for m in g["detail"].get("matches", [])]
        pool += [{"corpus": g["corpus_id"], "task": g["task_id"], "kind": "额外疑似", **x}
                 for x in g["detail"].get("extra_suspect", [])]
    if not pool:
        print("无阅卷明细——先跑 grade_corpus.py 生成 grade_task*.json")
        return 1
    n = max(1, round(len(pool) * rate))
    sample = random.Random(SEED).sample(pool, n)
    print(f"# 阅卷仲裁人工抽查清单（抽样率 {rate}，{n}/{len(pool)} 条，种子 {SEED} 可复现）")
    print()
    print("| # | 语料 | 任务 | 类别 | 条目 | 仲裁判定 | 仲裁理由 | 人工复核(同意/改判) | 备注 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(sample, 1):
        item = r.get("id") if r["kind"] == "对齐" else r.get("case")
        verdict = r.get("verdict", "extra_suspect")
        reason = (r.get("reason") or "").replace("|", "/")[:60]
        print(f"| {i} | {r['corpus']} | {r['task']} | {r['kind']} | {item} | {verdict} | {reason} |  |  |")
    print()
    print("复核口径：对齐条目=证人断言意图是否真落在该植入缺陷上；额外疑似=是否为出题人没想到的真缺陷"
          "（是→二次归类为检出新缺陷，否→误报）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
