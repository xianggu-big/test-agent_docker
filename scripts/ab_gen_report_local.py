"""gen_report 本地 LLM A/B 质量对账（2026-11-14，零 API）。

动机：llm_calls 台账显示 gen_report 占全链 DeepSeek token 25.6%（第二大头），
verdict 由确定性 judge 产出、叙述只是渲染——候选切本地。用户纪律=质量优先，
效果打折就算了。

设计（同输入双臂，零生产写入）：
- DB 拷贝到 tmp（sqlite backup API）——gen_report 的落库 UPDATE 只写副本；
- A 臂=库里已有叙述（DeepSeek 产物，当时 review 通过）；B 臂=本地 qwen3-4b 同
  metrics/rtm 重新生成（make_chat 打桩到 ollama /v1，log_llm_call 置空防污染台账）；
- 客观度量=review_rules 数字闸违规数（数字编造/防造假红线逐项比对）+ 生成速度；
- 主观度量=并排 md 给用户肉眼比。

用法：python scripts/ab_gen_report_local.py --tasks 1001,996,990
产物：data/reports/ab_gen_report/{ab_results.json, task_N_A.md, task_N_B.md}
"""
import argparse
import json
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "reports" / "ab_gen_report"
LOCAL_MODEL = "qwen3-4b-inst-test"
LOCAL_BASE = "http://localhost:11434/v1/chat/completions"


class LocalShim:
    """make_chat 打桩：同签名（.invoke([("system",..),("user",..)])→.content），
    直打 ollama OpenAI 兼容端点；记账 usage+耗时供速度对比。"""

    def __init__(self):
        self.calls = []

    def invoke(self, prompts):
        messages = [{"role": role, "content": content} for role, content in prompts]
        body = json.dumps({"model": LOCAL_MODEL, "messages": messages,
                           "temperature": 0.2}).encode("utf-8")
        req = urllib.request.Request(LOCAL_BASE, body,
                                     {"Content-Type": "application/json"})
        t0 = time.time()
        resp = json.load(urllib.request.urlopen(req, timeout=900))
        dt = time.time() - t0
        usage = resp.get("usage") or {}
        self.calls.append({"seconds": round(dt, 1),
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "completion_tokens": usage.get("completion_tokens")})
        rec = resp["choices"][0]["message"]["content"]

        class _R:
            content = rec
        return _R


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="1001,996,990")
    args = ap.parse_args()
    tids = [int(x) for x in args.tasks.split(",")]

    OUT.mkdir(parents=True, exist_ok=True)
    ab_db = OUT / "ab.db"
    if ab_db.exists():
        ab_db.unlink()
    src = sqlite3.connect(ROOT / "data" / "test_agent.db")
    dst = sqlite3.connect(ab_db)
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row

    # 打桩：本地臂不碰真实路由/台账（evaluate 内部 import 在调用时解析，patch 模块属性即可）
    from test_agent.skills import evaluate, llm_client, llm_profile
    shim = LocalShim()
    llm_client.make_chat = lambda **kw: shim          # noqa: F841 — gen_report 调用时取
    llm_profile.effective_key = lambda x=None: "ab-local-shim"
    evaluate.log_llm_call = lambda *a, **k: None

    from test_agent.skills.evaluate import (collect_metrics, gen_report,  # noqa: E402
                                            review_rules)

    results = []
    for tid in tids:
        metrics = collect_metrics(dst, tid)
        if metrics.get("no_report"):
            results.append({"task": tid, "skip": "no_report"})
            continue
        latest = dst.execute("SELECT matrix FROM rtm_maps WHERE task_id=?"
                             " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        rtm = {"matrix": json.loads(latest["matrix"]) if latest else []}
        verdict_row = dst.execute(
            "SELECT verdict FROM evaluation_rounds WHERE task_id=? AND level='L1'"
            " ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        verdict = verdict_row["verdict"] if verdict_row else ""
        stored = dst.execute("SELECT narrative FROM test_reports WHERE id=?",
                             (metrics["report_id"],)).fetchone()

        # A 臂：库内 DeepSeek 叙述（当时 review 通过的产物）
        narr_a = stored["narrative"] or ""
        rules_a = review_rules(narr_a, metrics, verdict)
        # B 臂：本地 qwen 同输入重生成（写副本库，不碰生产）
        shim.calls.clear()
        narr_b = gen_report(dst, tid, metrics, rtm)
        rules_b = review_rules(narr_b, metrics, verdict)

        (OUT / f"task_{tid}_A_deepseek.md").write_text(narr_a, encoding="utf-8")
        (OUT / f"task_{tid}_B_local.md").write_text(narr_b, encoding="utf-8")
        results.append({
            "task": tid, "verdict": verdict, "failure_kind": metrics.get("failure_kind"),
            "armA_deepseek": {"chars": len(narr_a), "rules_violations": rules_a},
            "armB_local": {"chars": len(narr_b), "rules_violations": rules_b,
                           "gen": list(shim.calls)},
            "same_input": True,
        })
        print(f"task={tid} A违规={len(rules_a)} B违规={len(rules_b)} "
              f"B耗时={sum(c['seconds'] for c in shim.calls):.0f}s "
              f"Btokens={sum(c['completion_tokens'] or 0 for c in shim.calls)}", flush=True)

    (OUT / "ab_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ab] 完成 → {OUT / 'ab_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
