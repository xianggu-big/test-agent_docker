r"""任务卡-D+ 真机演示（2026-11-10）：差分与快照两条新 oracle 路线。

演示一（CLI 差分）：data/demo/cli-diff-demo 的 v1（>50）vs v2（>=50）——
输出含时间戳/路径/uuid/浮点/时长五类噪声，验证"1 处真实行为差异、0 假阳性"；
参照物双跑确定性校验 + 检出复核（双向判定工程化）全程参与。

演示二（DOM 快照）：687 类项目（D:\c学习数据结构\test_1_学生管理系统 的
index.html）录基线 → 改一个元素 class → 结构 diff 定位到差异路径；
顺带验证基线更新安全边界（无确认拒绝）。

产物：data/reports/dplus/dplus_routes_demo_<ts>.json
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 旗标显式开启（demo 即显式动作；默认关=两条路线被 route_plan 剔除）
import os  # noqa: E402
os.environ["MTA_DIFF_ROUTE"] = "1"
os.environ["MTA_SNAPSHOT_ROUTE"] = "1"

from test_agent import config  # noqa: E402
from test_agent.skills.diff_route import run_cli_diff  # noqa: E402
from test_agent.skills.snapshot_route import (capture_dom_structure, compare,  # noqa: E402
                                              compare_dom, load_baseline,
                                              record_baseline, update_baseline)

OUT_DIR = ROOT / "data" / "reports" / "dplus"
PROJECT_687 = Path(r"D:\c学习数据结构\test_1_学生管理系统\index.html")


def demo_diff() -> dict:
    v1 = ROOT / "data" / "demo" / "cli-diff-demo" / "v1"
    v2 = ROOT / "data" / "demo" / "cli-diff-demo" / "v2"
    rep = run_cli_diff(v1, v2, [["demo.py"]])
    s = rep["summary"]
    print("[diff] summary:", json.dumps(s, ensure_ascii=False))
    for c in rep["cases"]:
        print("  kind:", c["kind"], "| 差异行:")
        for d in c["line_diffs"]:
            print(f"    L{d['line_no']}: v1={d['current']!r} v2={d['reference']!r}")
    assert s["diff_introduced"] == 1, f"应恰 1 处真实差异（实际 {s['diff_introduced']}）"
    kinds = {d for c in rep["cases"] for d in [c["kind"]]}
    assert kinds == {"stdout_diff"}, kinds
    noisy = c["cur_norm"]
    for tok in ("<TS>", "<PATH>", "<ID>", "<DUR>", "<F>"):
        assert tok in noisy, f"归一化占位 {tok} 未出现（噪声未被吞）"
    print("[diff] 验收：恰 1 处真实差异、0 假阳性、五类噪声全部占位化 ✓")
    return rep


def demo_snapshot() -> dict:
    html = PROJECT_687.read_text(encoding="utf-8")
    tree = capture_dom_structure(html)
    key = "dplus_demo_687"
    # 首跑只录不判
    if load_baseline(key, "index_dom") is None:
        record_baseline(key, "index_dom", tree)
        print("[snapshot] 首跑：基线已录（只录不判语义）")
    base = load_baseline(key, "index_dom")
    # 无确认改基线必须被拒（防造假安全边界）
    try:
        update_baseline(key, "index_dom", {"tampered": True})
        raise AssertionError("无确认更新未被拒绝——安全边界失效")
    except RuntimeError as e:
        print("[snapshot] 无确认更新被拒 ✓:", str(e)[:40], "…")
    # 改一个元素的 class → diff 定位
    import re
    m = re.search(r'class="([^"]+)"', html)
    old_cls = m.group(1)
    mutated_html = html.replace(f'class="{old_cls}"', f'class="{old_cls} dplus-mutated"', 1)
    cur_tree = capture_dom_structure(mutated_html)
    rep = compare_dom(base, cur_tree)
    print("[snapshot] class 变更 diff:", json.dumps(
        [{k: d[k] for k in ("path", "kind")} for d in rep["diffs"][:3]],
        ensure_ascii=False))
    assert not rep["same"] and any(d["path"].endswith(".class") for d in rep["diffs"])
    # 对照：改回原样 → zero diff（重构不改行为=快照 oracle 的核心价值）
    rep_same = compare_dom(base, capture_dom_structure(html))
    assert rep_same["same"], "未改动的页面不应有 diff（对照）"
    print("[snapshot] 对照组：未改动页面 zero diff ✓ | class 变更定位到", rep["diffs"][0]["path"])
    return {"class_diff": rep["diffs"][:3], "control_same": rep_same["same"],
            "baseline_nodes": _count_nodes(base)}


def _count_nodes(tree: dict) -> int:
    return 1 + sum(_count_nodes(c) for c in tree.get("children", []))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    d, s = demo_diff(), demo_snapshot()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"dplus_routes_demo_{ts}.json"
    out.write_text(json.dumps({"diff": d, "snapshot": s,
                               "wall_s": round(time.monotonic() - t0, 1)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已落档 {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
