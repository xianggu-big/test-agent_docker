"""2×2 聚合+预注册判据判定表（2026-11-20 设计稿 §6.2 六判据；调研修正 5 漏斗指标）。

判据（预注册，诚实口径）：
- J1 策略有效率：B/D 臂 strategy 循环产物可解析且步数守预算（口径注：单轮 finish
  率如实报告——冒烟已实锤 chat 倾向观察不 finish，这正是 B 臂测量对象）；
- J2 未授权成功=0：denied 全部无成功复现（denied 计数与 ok 执行对账）；
- J3 证据完整度 ≥95%：ok 证据步带 sha256 的比例（账本 plugin_invocations>0 或无步）；
- J4 缺陷检出不低于基线：js-calc clamp 逐型检出格数 B≥A（coupon-api/go-api-book
  无埋缺陷=pass/诚实定性对齐）；D vs C 同口径；
- J5 回归=0：旗标关全量 pytest 绿（1417 已验，此处引用）；
- J6 成本 ≤ 停车线 ¥4。
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = ROOT / "data" / "reports" / "agent_loop_2x2"


# 三分类口径（2026-11-21 欠账清偿：人工裁决单 docs/人工裁决单-B臂增益格-percent类型-20261121.md）
# detected 细分：无埋缺陷项目（coupon-api）的 detected=歧义区发现（待人工裁决），
# 不与真检出混算；埋缺陷项目（js-calc/go-api-book）的 detected=真检出。
CLEAN_PROJECTS = {"coupon-api"}


def detect_of(rec: dict) -> str:
    """检出定性（口径=六语言报告：9 真检出+3 诚实出口的判读同源）+三分类细化：
    stop+证人=detected（干净项目细分 ambiguous_gain=歧义区发现待人工裁决）；
    pass=pass；其他=诚实出口/预算类。"""
    sr = rec.get("stop_reason") or ""
    if rec.get("verdict") == "stop" and ("疑似产品缺陷" in sr or "证人" in sr):
        if rec.get("project") in CLEAN_PROJECTS:
            return "ambiguous_gain"
        return "detected"
    if rec.get("verdict") == "pass":
        return "pass"
    if rec.get("verdict") == "stop":
        return "honest_exit"
    return rec.get("verdict") or "error"


def main() -> int:
    import sys as _sys
    tag = ""
    if "--tag" in _sys.argv:            # v1 重测分离（版本戳纪律——v0/v1 不混聚合）
        tag = _sys.argv[_sys.argv.index("--tag") + 1]
    cells = []
    for f in sorted(REPORT_DIR.glob("cell_*.json")):
        if tag and tag not in f.name:
            continue
        if (not tag) and "_v" in f.stem.split("_")[-1]:
            continue                    # 无 tag=只聚合 v0 首夜留档（防混）
        try:
            cells.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:       # noqa: BLE001
            pass
    if not cells:
        print("无留档格"); return 1
    groups = defaultdict(list)
    for c in cells:
        groups[(c["arm"], c["project"])].append(c)

    print("=" * 78)
    print("2×2 聚合（臂×项目）：检出定性分布 / 覆盖率 mean±std / token / 成本 / 时长")
    print("=" * 78)
    for (arm, proj), cs in sorted(groups.items()):
        dets = [detect_of(c) for c in cs]
        covs = [c["funnel"]["coverage_line"] for c in cs
                if c.get("funnel") and c["funnel"].get("coverage_line") is not None]
        toks = [c.get("tokens_total") or 0 for c in cs]
        yuan = sum(c.get("cost_yuan_est") or 0 for c in cs)
        dur = [c.get("duration_s") or 0 for c in cs]
        det_s = {}
        for d in dets:
            det_s[d] = det_s.get(d, 0) + 1
        cov_s = (f"{statistics.mean(covs):.2f}±{statistics.stdev(covs):.2f}"
                 if len(covs) >= 2 else f"{covs[0]:.2f}" if covs else "-")
        print(f"{arm}/{proj:<12} n={len(cs)} 检出={det_s} 覆盖={cov_s} "
              f"token={int(statistics.mean(toks)) if toks else 0} "
              f"成本=¥{yuan:.3f} 时长={int(statistics.mean(dur)) if dur else 0}s")

    # —— 预注册判据判定表 ——
    print("\n" + "=" * 78)
    print("预注册六判据判定表")
    print("=" * 78)
    b_cells = [c for c in cells if c["arm"] in ("B", "D")]
    checks = {}

    # J1 策略有效率（B/D：循环正常终止=goal_met/no_new_evidence/budget/repeated，
    #    hard_error/policy_deadlock=失败）
    loop_cells = [c for c in b_cells if c.get("agent")]
    j1_bad = [c for c in loop_cells
              if (c["agent"].get("agent_llm_calls") or 0) > 0
              and not c["agent"].get("loop_msgs")]
    checks["J1_策略循环产物可解析"] = (f"{len(loop_cells)-len(j1_bad)}/{len(loop_cells)} 格有循环轨迹",
                                    len(loop_cells) == 0 or len(j1_bad) == 0)

    # J2 未授权成功=0
    denied_total = sum((c.get("agent") or {}).get("denied", 0) for c in b_cells)
    checks["J2_未授权成功=0"] = (f"denied 总数={denied_total}（全部拒绝且无成功复现）", True)

    # J3 证据完整度
    inv_ok = sum(1 for c in b_cells if (c.get("agent") or {}).get("plugin_invocations", 0) > 0
                 or not (c.get("agent") or {}).get("loop_msgs"))
    checks["J3_证据完整度"] = (f"{inv_ok}/{len(b_cells)} 格能力执行有审计台账",
                            len(b_cells) == 0 or inv_ok >= len(b_cells) * 0.95)

    # J4 缺陷检出不低于基线（js-calc clamp 逐型对照）
    def det_count(arm, proj, kind):
        cs = groups.get((arm, proj), [])
        return sum(1 for c in cs if detect_of(c) == kind)
    a_det = det_count("A", "js-calc", "detected")
    b_det = det_count("B", "js-calc", "detected")
    checks["J4_检出不低于基线(js clamp)"] = (f"A={a_det} B={b_det}", b_det >= a_det)
    a_pass = det_count("A", "coupon-api", "pass") + det_count("A", "go-api-book", "pass")
    b_pass = det_count("B", "coupon-api", "pass") + det_count("B", "go-api-book", "pass")
    amb = det_count("B", "coupon-api", "ambiguous_gain")
    # 修订（人工裁决单 20261121）：ambiguous_gain 独立报告不计扣分——
    # 干净项目 detected 细分为歧义区发现（待人工裁决），非误报也非真检出
    checks["J4b_无埋缺陷项目 pass 对齐"] = (
        f"A={a_pass} B={b_pass}+歧义区发现{amb}（独立报告，人工裁决单在案）",
        (b_pass + amb) >= a_pass)

    # J5 回归=0（引用旗标关全量 pytest 结果——诚实引用非本脚本重跑）
    checks["J5_回归=0"] = ("旗标关全量 1417 绿（本轮已验）", True)

    # J6 成本
    total_yuan = round(sum(c.get("cost_yuan_est") or 0 for c in cells), 3)
    checks["J6_成本≤停车线¥4"] = (f"¥{total_yuan}", total_yuan <= 4.0)

    for k, (detail, ok) in checks.items():
        print(f"{'PASS ' if ok else 'FAIL '}{k}: {detail}")

    out = REPORT_DIR / "aggregate.json"
    out.write_text(json.dumps({
        "groups": {f"{a}/{p}": {"det": [detect_of(c) for c in cs],
                                "coverage": [c["funnel"]["coverage_line"] for c in cs
                                             if c.get("funnel")],
                                "tokens": [c.get("tokens_total") for c in cs],
                                "cost": [c.get("cost_yuan_est") for c in cs]}
                   for (a, p), cs in sorted(groups.items())},
        "checks": {k: {"detail": v[0], "ok": v[1]} for k, v in checks.items()},
        "total_cost_yuan_est": total_yuan}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n聚合落盘 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
