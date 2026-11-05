# -*- coding: utf-8 -*-
"""P3·DeepSeek JSON mode 试点实测（docs/防御矩阵-prompt与非prompt措施-20261105.md §二 P3）。

问题：response_format={"type":"json_object"} 在「长输出压力」（20 条用例级，历史截断口径）
下可解析率是否优于现状宽容解析；JSON mode 与长输出的兼容性实测拍板——
可行则 A3/A4/A8 三类格式漂移从容错升级为结构保证，不可行则维持宽容解析现状。
口径：同一提示词（真实 generate 路径 build_user_prompt，20 条件）双臂各 n=3：
  对照=现状（无 response_format），实验=JSON mode；判据=_parse_cases 可解析+用例数。
成本预算：6 次调用 × ~2-3k token ≈ 0.03 元级；402 如实 blocked 不重试美化。
留档：data/reports/p3_json_mode_probe.json（原始输出留 llm_parse_fail/ 同级 p3_raw/）。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_agent import config
from test_agent.skills.gen_tests import _parse_cases
from test_agent.skills.llm_client import make_chat
from test_agent.skills.prompt_library import SYSTEM_PROMPT, build_user_prompt

N_PER_ARM = 3
REPORT = Path("data/reports/p3_json_mode_probe.json")
RAW_DIR = Path("data/reports/p3_raw")

# 被测模块：纯函数报价计算（与 checker 同形态=函数直调，输出 token 全部压在用例上）
SOURCE = '''"""订单报价模块：金额计算与折扣规则。"""
def quote_total(qty: int, unit_price: float, discount_pct: float = 0) -> float:
    """数量×单价，整单折扣百分比（0-50），四舍五入到分。qty<=0 抛 ValueError。"""
    if qty <= 0:
        raise ValueError("qty must be positive")
    if not (0 <= discount_pct <= 50):
        raise ValueError("discount out of range")
    total = qty * unit_price * (1 - discount_pct / 100)
    return round(total, 2)

def tier_discount(qty: int) -> int:
    """阶梯折扣：1-9 件 0%，10-49 件 5%，50-99 件 10%，>=100 件 15%。"""
    if qty >= 100:
        return 15
    if qty >= 50:
        return 10
    if qty >= 10:
        return 5
    return 0

def format_receipt(qty: int, unit_price: float) -> str:
    """格式：'N x P = T (D% off)'，T 含阶梯折扣，两位小数。"""
    d = tier_discount(qty)
    t = quote_total(qty, unit_price, d)
    return f"{qty} x {unit_price} = {t} ({d}% off)"
'''

CONDITIONS = (
    [{"condition": f"quote_total 数量边界 qty={q}", "priority": "高"} for q in (0, 1, 9, 10, 49, 50, 99, 100)]
    + [{"condition": f"tier_discount 阶梯边界 qty={q}", "priority": "高"} for q in (0, 1, 9, 10, 49, 50, 99, 100)]
    + [{"condition": "quote_total 折扣 0/25/50/51 四点（51 抛 ValueError）", "priority": "高"},
       {"condition": "quote_total 四舍五入到分（0.005 尾差）", "priority": "中"},
       {"condition": "format_receipt 格式串精确比对（含折扣）", "priority": "中"},
       {"condition": "qty 非整数类型错误路径", "priority": "低"}]
)


def run_arm(label: str, json_mode: bool) -> list[dict]:
    kwargs = dict(model=config.DEEPSEEK_MODEL, api_key=os.environ["DEEPSEEK_API_KEY"],
                  base_url=config.DEEPSEEK_BASE_URL, temperature=0.2, timeout=120,
                  max_retries=2, phase="p3_probe")
    if json_mode:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    llm = make_chat(**kwargs)
    prompt = build_user_prompt([("pricing.py", SOURCE)], "按模块 docstring 与需求设计用例",
                               conditions=CONDITIONS, defense_v2=True)
    out = []
    for i in range(N_PER_ARM):
        t0 = time.time()
        try:
            resp = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])
        except Exception as e:  # noqa: BLE001 —— 402/超时如实记录不重试美化
            out.append({"arm": label, "run": i + 1, "error": type(e).__name__,
                        "detail": str(e)[:200]})
            continue
        dt = round(time.time() - t0, 1)
        raw = resp.content
        (RAW_DIR).mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{label}_r{i + 1}.txt").write_text(raw, encoding="utf-8")
        parsed = _parse_cases(raw)
        usage = getattr(resp, "usage_metadata", None) or {}
        out.append({
            "arm": label, "run": i + 1, "latency_s": dt,
            "raw_chars": len(raw),
            "json_loads_ok": _try_json(raw),
            "parse_ok": parsed is not None,
            "cases": len(parsed["cases"]) if parsed and isinstance(parsed, dict) else 0,
            "tokens": (usage.get("output_tokens"), usage.get("input_tokens")),
        })
        print(f"[{label} r{i + 1}] {out[-1]}")
    return out


def _try_json(raw: str) -> bool:
    try:
        json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("BLOCKED: DEEPSEEK_API_KEY 未设置（诚实退出码 3）")
        return 3
    result = {"n_per_arm": N_PER_ARM, "conditions": len(CONDITIONS),
              "control": run_arm("control", False), "json_mode": run_arm("json", True)}
    for arm in ("control", "json_mode"):
        runs = [r for r in result[arm] if "error" not in r]
        result[f"{arm}_summary"] = {
            "runs_ok": len(runs),
            "parse_rate": f"{sum(r['parse_ok'] for r in runs)}/{len(runs)}" if runs else "0/0",
            "avg_cases": round(sum(r["cases"] for r in runs) / len(runs), 1) if runs else 0,
            "avg_latency_s": round(sum(r["latency_s"] for r in runs) / len(runs), 1) if runs else 0,
        }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in result if k.endswith("_summary")},
                     ensure_ascii=False, indent=2))
    print(f"留档: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
