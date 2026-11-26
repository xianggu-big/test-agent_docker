# -*- coding: utf-8 -*-
"""agentloop_gates_audit.py——A4（P1b 三闸对照）取证脚本（零 LLM）。

对 tag=gates 批的每格取证：
  ① run 行 config_json.flags 三闸开关落库确认；
  ② summary_json/loop_msgs 里 [env-gate]/[diff-gate]/flaky 痕迹；
  ③ 诊断类别（diagnose 节点 kind）与 D-K 对照；
  ④ 工作区无 .git → diff 闸 no_git 如实记。
输出：stdout 逐格表 + data/reports/agentloop_scale/gates_audit.json

用法：python -I scripts/agentloop_gates_audit.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CELLS = ROOT / "data" / "reports" / "agentloop_scale" / "cells"
OUT = ROOT / "data" / "reports" / "agentloop_scale" / "gates_audit.json"


def main() -> int:
    import re
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    rows = []
    for p in sorted(CELLS.glob("fixture_gates_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        tid = d["task_id"]
        rec = {"file": p.name, "cls": d["cls"], "task_id": tid,
               "status": d["status"], "triggered": d.get("triggered"),
               "diagnose_steps": d.get("diagnose_steps")}
        run = conn.execute("SELECT id, config_json, summary_json FROM agent_runs "
                           "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if run is None:
            rec["note"] = "无 run 行"
            rows.append(rec)
            continue
        cfg = {}
        try:
            cfg = (json.loads(run["config_json"] or "{}").get("flags") or {})
        except (ValueError, TypeError):
            pass
        rec["flags"] = {k: cfg.get(k) for k in
                        ("agent_loop_enabled", "env_gate", "diff_gate", "flaky_reruns")}
        # 三闸痕迹真源=agent_messages（msg() 落会话消息；summary_json 不含 loop_msgs）
        msgs = conn.execute(
            "SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id=m.session_id "
            "WHERE s.task_id=? AND (m.content LIKE '%env-gate%' OR m.content LIKE '%diff-gate%' "
            "OR m.content LIKE '%flaky%') ORDER BY m.id", (tid,)).fetchall()
        texts = [x["content"] for x in msgs]
        rec["env_gate_pass"] = any("金丝雀通过" in t for t in texts)
        rec["env_gate_fail"] = any("金丝雀未通过" in t for t in texts)
        diff_msgs = [t for t in texts if "[diff-gate]" in t]
        rec["diff_gate_msg"] = diff_msgs[-1][:120] if diff_msgs else None
        rec["diff_no_git"] = any("no_git" in t for t in texts)
        rec["flaky_marked"] = any("flaky 连跑" in t for t in texts)
        # 诊断类别（diagnose finish 的 kind）
        kind_row = conn.execute(
            "SELECT s.decision_json FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id "
            "WHERE r.task_id=? AND s.mode='diagnose' AND s.action_type='finish' "
            "ORDER BY s.step_no DESC LIMIT 1", (tid,)).fetchone()
        if kind_row and kind_row["decision_json"]:
            try:
                dj = json.loads(kind_row["decision_json"])
                rec["diagnosis_kind"] = (dj.get("diagnosis") or {}).get("kind")
            except (ValueError, TypeError):
                rec["diagnosis_kind"] = None
        else:
            rec["diagnosis_kind"] = None
        rec["first_kind"] = d.get("notes", {}).get("stage1_status")
        rows.append(rec)

    agg = {
        "n_cells": len(rows),
        "flags_on": sum(1 for r in rows
                        if r.get("flags", {}).get("env_gate")
                        and r.get("flags", {}).get("diff_gate")),
        "env_gate_pass": sum(1 for r in rows if r.get("env_gate_pass")),
        "env_gate_fail": sum(1 for r in rows if r.get("env_gate_fail")),
        "diff_gate_fired": sum(1 for r in rows if r.get("diff_gate_msg")),
        "diff_no_git": sum(1 for r in rows if r.get("diff_no_git")),
        "flaky_marked": sum(1 for r in rows if r.get("flaky_marked")),
        "diagnosis_kinds": {},
    }
    for r in rows:
        k = r.get("diagnosis_kind") or "（未触发诊断）"
        agg["diagnosis_kinds"][k] = agg["diagnosis_kinds"].get(k, 0) + 1

    OUT.write_text(json.dumps({"per_cell": rows, "aggregate": agg},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(agg, ensure_ascii=False, indent=1))
    for r in rows:
        print(f"  {r['file']} tid={r['task_id']} 触发={r.get('triggered')} "
              f"诊断步={r.get('diagnose_steps')} kind={r.get('diagnosis_kind')} "
              f"env={'P' if r.get('env_gate_pass') else ('F' if r.get('env_gate_fail') else '-')}"
              f" diff={'有' if r.get('diff_gate_msg') else '-'}"
              f"{'(no_git)' if r.get('diff_no_git') else ''}"
              f" flaky={'是' if r.get('flaky_marked') else '-'}")
    print(f"\n[written] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
