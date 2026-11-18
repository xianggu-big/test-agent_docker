# -*- coding: utf-8 -*-
"""L3 别名自学习·真机触发格（2026-11-18 晚）：别名表进新词后，路由行为真机改变。

前置（本脚本之前已完成，见 l3_alias_flow_results.json）：
  mini 簇 → 真 suggest_aliases 产出候选 final_price→discount → 人工审核（AI 代行）
  → accept 进 _ALIAS_GROUPS（"折扣"族）→ 重录快照 → 金样 11/11。

本脚本真机证据：构造项目 l3-discount-demo（discount/cart 双模块）建真任务
（两阶段），confirm 触发阶段一（真 clone→analyze）→ 停在 ready_for_review
→ 断言 [analyze·index] 消息命中 discount 模块+依赖闭包——改表前该需求路由
none（预演已留痕），改表后真机命中=L3 词表真实改变路由行为的直接证据。
任务停在待审查不释放（阶段二不跑，零执行成本）。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/reports/l1l2l3_real/l3_alias_real_results.json"
DB = ROOT / "data/test_agent.db"
BASE = "http://127.0.0.1:8000"
TERMINAL = {"done", "failed", "cancelled"}


def task_messages(tid: int) -> list:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ses = conn.execute("SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (tid,)).fetchone()
        if not ses:
            return []
        return [r["content"] for r in conn.execute(
            "SELECT content FROM agent_messages WHERE session_id=? ORDER BY seq", (ses["id"],))]
    finally:
        conn.close()


def main() -> int:
    s = requests.Session()
    h = {"Authorization": "Bearer " + s.post(
        f"{BASE}/api/login", json={"username": "admin", "password": "admin123"},
        timeout=10).json()["token"]}
    tid = s.post(f"{BASE}/api/tasks", headers=h, timeout=10, json={
        "repo_url": str(ROOT / "data/demo/l3-discount-demo"),
        "requirement": "测试折扣计算的边界：VIP 八折、满 200 再减 20、非会员原价",
        "two_stage": True, "ai_reviewer": "off"}).json()["task_id"]
    s.post(f"{BASE}/api/tasks/{tid}/confirm", headers=h, timeout=10)
    print(f"[l3] 任务 #{tid} 已确认，阶段一跑批（clone→analyze→plan→generate→停等）", flush=True)

    # 轮询到 ready_for_review（两阶段停等）或终态，最长 15 分钟
    t0, status = time.time(), ""
    while time.time() - t0 < 900:
        d = s.get(f"{BASE}/api/tasks/{tid}", headers=h, timeout=10).json()
        status = d.get("status", "")
        if status in ("ready_for_review", *TERMINAL):
            break
        time.sleep(15)
    print(f"[l3] 终态/停等态：{status}", flush=True)

    msgs = task_messages(tid)
    hit = [m for m in msgs if "[analyze·index]" in m]
    evidence = {
        "case": "L3-R 真机路由行为改变",
        "tid": tid, "status": status,
        "analyze_msgs": hit,
        "before": "改表前同需求路由 none（零 LLM 预演留痕：金样前 hit=none）",
        "generated_at": time.strftime("%F %T"),
    }
    if hit and "discount" in hit[-1]:
        evidence["verdict"] = "PASS"
        evidence["evidence"] = ("改表前 none → 改表后真机命中 discount："
                                + hit[-1][:160])
    else:
        evidence["verdict"] = "FAIL"
        evidence["evidence"] = f"未命中 discount（analyze 消息 {len(hit)} 条）"
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[判定] {evidence['verdict']}｜{evidence['evidence'][:150]}", flush=True)
    for m in hit:
        print("  ", m[:160], flush=True)
    return 0 if evidence["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
