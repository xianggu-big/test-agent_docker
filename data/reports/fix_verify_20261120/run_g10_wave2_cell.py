# -*- coding: utf-8 -*-
"""G10 Wave2 驱动器单格真机验收（2026-11-20 设计文档 §2）。

验证 C 包（dbf44dd）：条件快照恢复正确 + 留档完整（task_id/utf-8）。
只跑单格即止——Wave2 300 格冻结不解冻（跑完本格后驱动器不再被调用）。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent / "g10_wave2_cell.json"


def main():
    spec = importlib.util.spec_from_file_location(
        "rw2", ROOT / "data" / "reports" / "e10" / "run_wave2.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    # 1. 从 wave2 冻结清单取首个基线格（manifest 已冻结=唯一真源）
    tids = json.loads(m.MANIFEST.read_text(encoding="utf-8"))
    baseline = tids[0]
    rec = {"gid": "G10", "baseline_tid": baseline, "steps": []}

    def step(name, ok, detail):
        rec["steps"].append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # 2. 条件快照恢复
    cond = m.load_baseline_condition(baseline)
    step("条件快照恢复", cond is not None and cond.get("arm") and cond.get("corpus"),
         f"arm={cond.get('arm') if cond else None} corpus={cond.get('corpus') if cond else None} "
         f"source={cond.get('source') if cond else None}")
    if cond is None:
        rec["pass"] = False
        OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # 3. 单格真跑（真 worker 子进程，真 LLM；结果即落档）
    rec_cell = m.run_cell(baseline, seq=0, round_no=1, condition=cond)
    rec["cell"] = {k: rec_cell.get(k) for k in
                   ("status", "task_id", "condition_source", "tokens_spent", "elapsed_s")}
    print(f"[cell] {rec['cell']}", flush=True)

    # 4. 留档完整性判定
    ok_task = rec_cell.get("task_id") is not None   # RESULT_MARK 带 task_id（done 格）
    ok_cond = rec_cell.get("condition_source") == cond.get("source")
    ok_enc = rec_cell.get("status") in ("done", "failed")   # 非 error/timeout=编码根修生效
    step("task_id 留档", ok_task, f"task_id={rec_cell.get('task_id')}")
    step("条件快照来源落档", ok_cond, f"condition_source={rec_cell.get('condition_source')}")
    step("编码根修（非 error/timeout 出口）", ok_enc, f"status={rec_cell.get('status')}")

    # 5. 结果写 wave2_results.json（断点续跑语义：done 格续跑跳过——300 格仍冻结，
    # 本格 done 后其余格不会被自动消费，因为驱动器无人再点火）
    rec["pass"] = all(s["pass"] for s in rec["steps"])
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nG10 判定: {'PASS' if rec['pass'] else 'FAIL'} → {OUT}", flush=True)
    print("注意：Wave2 300 格仍冻结——本格仅验证驱动器，续跑需用户拍板", flush=True)
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
