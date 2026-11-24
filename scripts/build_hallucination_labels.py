# -*- coding: utf-8 -*-
"""T9 幻觉率标注材料构建器（学院达标，2026-11-25）——零 LLM。

输入：48 格 Agent Loop 留档的 loop_msgs / agent.strategy.msg / agent.diagnose
（对码实锤：why 全文未落库——2×2 驱动器只存了一行式摘要消息，设计稿 §12-T9
的输入假设按应变协议修订为"消息级 claim 预标"；深度预标待 T10 后
agent_steps.decision_json 全量，见台账）。

对每条消息 claim 解析断言（stop 词 / 步数 / 优先级数）→ 形式一致性预标：
  supported（形式一致）/ contradicted（自相矛盾）/ unverifiable（无第二源）。
证据指针 = cell 文件路径 + 消息序号（可点击打开）。
final_label 留空待用户抽检（≥20% 抽样清单一并产出）——未确认前一律称"预标值"。
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CELL_GLOB = str(ROOT / "data" / "reports" / "agent_loop_2x2" / "cell_*.json")
OUT_DIR = ROOT / "data" / "reports" / "hallucination_labeling"

KNOWN_STOPS = ("goal_met", "budget", "repeated_action", "policy_deadlock",
               "no_new_evidence", "hard_error")


def parse_msg(msg: str) -> dict:
    """[strategy]/[diagnose] 行 → 断言字典。"""
    out: dict = {"raw": msg}
    m = re.search(r"决策循环 (\w+)", msg)
    if m:
        out["stop_claim"] = m.group(1)
    m = re.search(r"（(\d+) 步）", msg)
    if m:
        out["steps_claim"] = int(m.group(1))
    m = re.search(r"策略 (\d+) 项优先级", msg)
    if m:
        out["priorities_claim"] = int(m.group(1))
    return out


def pre_label(claim: dict) -> tuple[str, str]:
    """形式一致性预标：stop 词在已知集合、数字非负、goal_met 才可能有优先级>0。

    返回 (label, reason)。注意：这是"消息自洽"检查，不是真值核对——
    无第二源（决策原文未落库），真值核对只能 strict-unverifiable。"""
    stop = claim.get("stop_claim")
    if stop is None:
        return "unverifiable", "无可解析断言（非决策循环行）"
    if stop not in KNOWN_STOPS:
        return "contradicted", f"stop 词 {stop!r} 不在已知停止原因集合（契约 §1.2）"
    steps = claim.get("steps_claim")
    if steps is not None and steps < 1:
        return "contradicted", "步数 <1 与循环机制矛盾（至少 1 步才停）"
    pri = claim.get("priorities_claim")
    if pri is not None and pri > 0 and stop in ("hard_error", "budget"):
        return "contradicted", f"{stop} 停止却声称有 {pri} 项优先级（goal_met 才有 payload）"
    return "supported", "形式一致（自洽；真值待 T10 decision_json 双源核对）"


def main() -> int:
    labels: list[dict] = []
    for f in sorted(glob.glob(CELL_GLOB)):
        cell = Path(f)
        s = json.load(open(f, encoding="utf-8"))
        ag = s.get("agent") or {}
        msgs: list[tuple[str, str]] = [("loop_msgs", m)
                                       for m in (ag.get("loop_msgs") or [])]
        st = (ag.get("strategy") or {}).get("msg")
        if st and ("loop_msgs", st) not in msgs:
            msgs.append(("strategy.msg", st))
        for src, msg in msgs:
            if not msg.startswith(("[strategy]", "[diagnose]")):
                continue
            claim = parse_msg(msg)
            label, reason = pre_label(claim)
            labels.append({
                "claim_id": f"{cell.name}#{len(labels)}",
                "source": src,
                "cell": cell.name,
                "arm": s.get("arm"), "project": s.get("project"), "n": s.get("n"),
                "claim_text": msg,
                "assertions": {k: v for k, v in claim.items() if k != "raw"},
                "claim_type": "C3-loop-summary",
                "pre_label": label,
                "pre_label_reason": reason,
                "evidence_pointer": f"data/reports/agent_loop_2x2/{cell.name} · {src}",
                "final_label": "",          # 留空待用户抽检
                "reviewer": "",
            })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    batch = "20261125"
    outp = OUT_DIR / f"labels_{batch}.json"
    outp.write_text(json.dumps({"batch": batch, "pre_label_only": True,
                                "note": "未获用户确认前只能称预标值（Q3 未拍板）",
                                "labels": labels}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # 抽样清单 ≥20%
    step = 5   # 固定每 5 抽 1：36 条 → 8 条 = 22%（≥20%）
    sample = [lb["claim_id"] for lb in labels[::step]]
    (OUT_DIR / f"sampling_{batch}.json").write_text(
        json.dumps({"rule": f"每 5 条抽 1（{len(sample)}/{len(labels)}={len(sample)/max(len(labels),1):.0%}）",
                    "sample_ids": sample}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    from collections import Counter
    dist = Counter(lb["pre_label"] for lb in labels)
    print(f"[t9] claims={len(labels)} 分布={dict(dist)} 抽样={len(sample)}")
    print(f"[t9] labels → {outp}")
    print(f"[t9] sampling → {OUT_DIR / f'sampling_{batch}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
