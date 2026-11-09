# -*- coding: utf-8 -*-
"""期 B 两臂对照（2026-11-09）：上下文示范蒸馏——有/无决策骨架范例对本地模型
路线选择的影响。零微调（期 C 只写设计）。

设计：
- ground truth = 期 A 确定性路由器（route_plan）的结果——LLM 不得越过前置条件，
  所以「准确率」=LLM top-1 是否等于确定性路由器的 top-1。
- 两臂同信息（同一份系统提示：路线清单摘要+同一份项目特征串），唯一变量=有无
  2 条决策骨架范例（观察特征→选路线→理由 三段式，非长 CoT）。
- 本地模型 qwen3-4b-inst-test（localhost:11434/v1，ctx 8192——token 成本即预算压力
  指标之一）。n≥3 次采样/臂，统计 top-1 准确率 + JSON 可解析率 + 平均 token。
"""
import json
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys_root = Path(__file__).resolve().parent.parent
sys_path = str(sys_root)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

SAMPLES = Path(sys_root / ".autonomous-loop" / "route_ab_samples.json")
OUT = Path(sys_root / "data" / "reports" / "route_ab")
OLLAMA = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen3-4b-inst-test"

ROUTE_MENU = """可选测试路线（id｜适用前置｜成本）：
- l1_unit_pytest｜主语言 python 且有 .py 源码｜medium
- l2_contract_http｜python 且检测到 HTTP 框架（fastapi/flask 等）｜medium
- l3_scenario｜python 且 HTTP 框架（且 L3 开关开）｜high
- l4_acceptance｜python 且 HTTP 框架（且 L4 开关开）｜high
- ui_static_dom｜有 js 源码（前端工程）｜medium
- ui_interaction_idempotency｜有 .html 且检测到可交互元素（button/submit）｜low
- db_assert｜有 .sql/.db 文件｜low
- grpc_contract｜有 .proto 文件｜medium
- static_scan｜兜底（任何项目可用，仅静态草稿不执行）｜low"""

EXEMPLARS = """## 决策范例（观察特征 → 选路线 → 理由）
范例一：
观察特征：主语言 unknown；2 个 .html 文件；检测到可交互元素（button/submit）与表单容器；无任何后端语言与框架。
选路线：主选 ui_interaction_idempotency。
理由：纯静态页没有语言执行链路，但 Playwright 可 file:// 真跑交互；幂等 oracle（状态不变量）不依赖 LLM 判定，成本 low。static_scan 仅兜底。
范例二：
观察特征：主语言 python；5 个 .py 文件；检测到 fastapi；3 个 .sql 文件。
选路线：主选 l2_contract_http，辅选 l1_unit_pytest 与 db_assert。
理由：HTTP 框架在场=契约测试可真跑（oracle 明确）；.py 充足支持单测；SQL 文件给 DB 断言靶子。三者 cost 可承受。"""

FEATURE_FMT = """项目特征（确定性提取）：主语言={language}；py 文件={py}；js 文件={js}；html 文件={html}；
proto 文件={proto}；sql 文件={sql}；表单/列表容器={has_forms}；可交互元素={has_interactive}；
HTTP 框架={has_http_framework}；package.json={has_package_json}；go.mod={has_go_mod}"""


def ask(body: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def build_prompt(sample: dict, with_exemplar: bool) -> str:
    parts = [ROUTE_MENU, "", FEATURE_FMT.format(**sample["features"]),
             "", f"需求：{sample['requirement']}", "",
             "任务：只输出一个 JSON 对象（无其他文字）："
             '{"primary_route": "<路线id>", "reason": "<一句话理由>"}']
    if with_exemplar:
        parts.insert(3, EXEMPLARS)
        parts.insert(4, "")
    return "\n".join(parts)


def one_run(sample: dict, with_exemplar: bool) -> dict:
    prompt = build_prompt(sample, with_exemplar)
    t0 = time.time()
    try:
        resp = ask({"model": MODEL, "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200})
        text = resp["choices"][0]["message"]["content"].strip()
        usage = resp.get("usage", {})
    except Exception as e:                      # noqa: BLE001 —— 本地服务瞬断如实记
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120], "latency_ms": 0,
                "prompt_tokens": 0, "completion_tokens": 0}
    m = re.search(r"\{.*\}", text, re.S)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
    return {"ok": parsed is not None and "primary_route" in parsed,
            "primary_route": (parsed or {}).get("primary_route"),
            "latency_ms": int((time.time() - t0) * 1000),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0)}


def main() -> int:
    n = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 3
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"with": [], "without": []}
    for arm, with_ex in (("with", True), ("without", False)):
        for run_i in range(n):
            for i, s in enumerate(samples):
                r = one_run(s, with_ex)
                r.update({"run": run_i, "sample_i": i, "gt_top": s["gt_top"],
                          "hit": r.get("primary_route") == s["gt_top"]})
                results[arm].append(r)
            print(f"[{arm}] run {run_i+1}/{n} 完成", flush=True)
    report = {}
    for arm, rows in results.items():
        ok = sum(1 for r in rows if r.get("ok"))
        hit = sum(1 for r in rows if r.get("hit"))
        pt = sum(r.get("prompt_tokens", 0) for r in rows)
        ct = sum(r.get("completion_tokens", 0) for r in rows)
        report[arm] = {
            "runs": len(rows), "json_ok": ok,
            "json_rate": round(ok / len(rows), 3) if rows else 0,
            "top1_hit": hit, "top1_acc": round(hit / len(rows), 3) if rows else 0,
            "prompt_tokens_avg": round(pt / len(rows), 1) if rows else 0,
            "completion_tokens_avg": round(ct / len(rows), 1) if rows else 0}
        dist = Counter(r.get("primary_route") for r in rows)
        report[arm]["answer_dist"] = dict(dist)
    report["delta"] = {
        "top1_acc_gain": round(report["with"]["top1_acc"] - report["without"]["top1_acc"], 3),
        "json_rate_gain": round(report["with"]["json_rate"] - report["without"]["json_rate"], 3),
        "prompt_tokens_delta": round(report["with"]["prompt_tokens_avg"]
                                     - report["without"]["prompt_tokens_avg"], 1)}
    out = {"model": MODEL, "n_arms": n, "samples": len(samples), "results": report}
    (OUT / f"route_ab_n{n}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
