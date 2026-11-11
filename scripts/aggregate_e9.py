"""E9 全量档波次聚合（扩展 aggregate_grid/aggregate_e68 的 E9 字段，任务包 §2 2:50-3:00 动作）。

用法：python scripts/aggregate_e9.py
读 data/reports/e9_full/e9a*.json（每格留档真源）+ 真库 review_decisions（只读），
输出进度报告 markdown 到 data/reports/e9_full/progress_<时间戳>.md：
各 Arm 完成数/累计 token/失败率/剩余量与预计剩余波次 + A1 组间检出对照 + A5 kill_rate。
零 LLM 零写入（真库只读）。红线：不改实验数字，负结果如实呈现。
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "reports" / "e9_full"

# 2026-11-10 口径：总上限 500 跑/总预算 6500 万 token；Arm 目标=11-06 ≈100 格档拍板
# （A1 前 17 组合×2 语料×3n=102 记 127 实跑；A2 90；A3 54；A5 50；A4 8×3×5=120 列后续）
TARGETS = {"A1": 102, "A2": 90, "A3": 54, "A4": 120, "A5": 50}
TOTAL_RUN_CAP = 500
TOTAL_TOKEN_BUDGET = 65_000_000
WAVE_THROUGHPUT_TOKENS_PER_H = 8_000_000  # 任务包单机 3h≈8000万 的校准口径


def load_cells() -> list[dict]:
    cells = []
    for f in sorted(OUT.glob("e9a*.json")):
        try:
            cells.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            cells.append({"arm": "?", "label": f.stem, "status": "unreadable", "error": str(e)})
    return cells


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "-"


def main() -> int:
    cells = load_cells()
    lines = [f"# E9 全量档进度报告（{time.strftime('%F %T')}）", ""]
    by_arm: dict[str, list] = defaultdict(list)
    for c in cells:
        by_arm[c.get("arm", "?")].append(c)

    total_done = total_tokens = 0
    lines += ["## 各 Arm 总览", "",
              "| Arm | 完成/目标 | 失败格 | 失败率 | 累计 token | 预算 | 剩余量 |",
              "|---|---|---|---|---|---|---|"]
    for arm in ("A1", "A2", "A3", "A4", "A5"):
        cs = by_arm.get(arm, [])
        done = [c for c in cs if c.get("status") == "done"]
        # 失败口径：真失败=error/budget_exhausted/unreadable；abandoned_prev_wave=中断留痕单列
        failed = [c for c in cs if c.get("status") in ("error", "budget_exhausted", "unreadable")]
        abandoned = [c for c in cs if c.get("status") == "abandoned_prev_wave"]
        toks = sum(int(c.get("tokens_spent") or 0) for c in cs)
        total_done += len(done)
        total_tokens += toks
        remain = TARGETS[arm] - len(cs)
        lines.append(f"| {arm} | {len(cs)}/{TARGETS[arm]} | {len(failed)} | "
                     f"{pct(len(failed), len(cs))} | {toks:,} | 总口径 65,000,000 | "
                     f"{remain} 格（另有中断留痕 {len(abandoned)}） |")
    lines += ["", f"- 全局：完成 {total_done} 格 / 累计 {total_tokens:,} token "
              f"（≈{total_tokens / 10000:.0f} 万）",
              f"- 预计剩余波次：按单波 {WAVE_THROUGHPUT_TOKENS_PER_H // 10000} 万 token 口径，"
              f"全包余量 ≈ "
              f"{max(0, TOTAL_TOKEN_BUDGET - total_tokens) / WAVE_THROUGHPUT_TOKENS_PER_H:.1f} 波",
              ""]

    a1 = by_arm.get("A1", [])
    done_a1 = [c for c in a1 if c.get("status") == "done" and c.get("flags")]
    if done_a1:
        lines += ["## Arm 1 组间对照（完成格）", "",
                  "| combo | 开关偏离 | n | verdict | passed/failed | 覆盖率 |",
                  "|---|---|---|---|---|---|"]
        for c in sorted(done_a1, key=lambda x: (x.get("combo_idx", 0), x.get("n", 0)))[:40]:
            fl = c["flags"]
            dev = [k.replace("_ENABLED", "") for k, v in fl.items()
                   if v != (k in ("GEN_METHODS_ENABLED", "PLAN_ENABLED",
                                  "COVERAGE_FEEDBACK_ENABLED"))]
            l1 = (c.get("legs") or {}).get("L1", {})
            lines.append(f"| c{c.get('combo_idx'):02d} | {','.join(dev) or '默认组'} | "
                         f"{c.get('n')} | {c.get('verdict')} | "
                         f"{l1.get('passed')}/{l1.get('failed')} | {l1.get('coverage_line')} |")
        lines.append("")

    a2 = by_arm.get("A2", [])
    done_a2 = [c for c in a2 if c.get("status") == "done"]
    if done_a2:
        lines += ["## Arm 2 代审三模式（corpus-001，L2-L4 仅 corpus-003 有效）", "",
                  "| mode | n | status | L1 verdict | passed/failed | 覆盖率 | 决策数 |",
                  "|---|---|---|---|---|---|---|"]
        for c in sorted(done_a2, key=lambda x: (x.get("ai_reviewer") or "",
                                                x.get("n") or 0)):
            lg = c.get("legs") or {}
            l1 = lg.get("L1") or {}

            def v(level):
                return (lg.get(level) or {}).get("verdict") or "-"
            rd = c.get("review_decisions_summary") or {}
            if isinstance(rd, dict) and rd:
                rd_total = sum(rd.values())
            else:  # 正式格 JSON 无决策摘要 → 真库现查（只读）
                try:
                    from test_agent.store.db import get_conn
                    conn = get_conn()
                    rd_total = conn.execute(
                        "SELECT COUNT(*) n FROM review_decisions WHERE task_id=?",
                        (c.get("task_id"),)).fetchone()["n"]
                    conn.close()
                except Exception:  # noqa: BLE001
                    rd_total = "-"
            lines.append(f"| {c.get('ai_reviewer')} | {c.get('n')} | {c.get('status')} | "
                         f"{v('L1') or l1.get('verdict') or '-'} | "
                         f"{l1.get('passed', c.get('passed'))}/{l1.get('failed', c.get('failed'))} | "
                         f"{l1.get('coverage_line', '-')} | {rd_total} |")
        lines.append("")

    a5 = by_arm.get("A5", [])
    done_a5 = [c for c in a5 if c.get("status") == "done" and c.get("mutation")]
    if done_a5:
        rates = [c["mutation"].get("kill_rate") for c in done_a5
                 if c["mutation"].get("kill_rate") is not None]
        if rates:
            mean = sum(rates) / len(rates)
            lines += ["## Arm 5 变异杀死率", "",
                      f"- 有效格 {len(rates)}，kill_rate 均值 {mean:.3f} "
                      f"（min {min(rates):.3f} / max {max(rates):.3f}）", ""]

    problems = [c for c in cells if c.get("status") not in ("done",)]
    if problems:
        lines += ["## 非正常格（如实留痕）", ""]
        for c in problems[:20]:
            lines.append(f"- {c.get('label')}: {c.get('status')} "
                         f"{(c.get('error') or '')[:120]}")
        lines.append("")

    report = OUT / f"progress_{time.strftime('%Y%m%d_%H%M')}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已落 {report}")
    print("\n".join(lines[:24]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
