# -*- coding: utf-8 -*-
"""L1/L2/L3 辅助急救层端到端真机验收驱动器（2026-11-18 设计方案 §4，三格 ≈¥1）。

时间：18:02 低价窗点火（硬规则：真机验收格禁高峰段；18:01 bugverify 批先入队，
本批错峰 1 分钟，单 worker 串行天然排队）。判定=预注册判据（设计方案 §4 表），
消息证据从真实库只读反查（agent_messages）。

预注册判据：
  E2E-1 go-rest-api "测所有 REST 接口"  → 消息含 "[analyze·index] 需求命中接口清单"
       且 count>=6；用例数>=端点数或有缺口说明（合法终态即可）
  E2E-2 nestjs "测试用户模块的边界"     → 消息含 "依赖闭包"；gen 摘要含
       "索引路由范围外"（scope 外文件进跳过台账）
  E2E-3 js-calc "测试支付模块的非法输入" → 消息含 "需求未命中索引——回退全库扫描"
       或 "[index·L2]"（诚实出口），终态合法（done/failed）
总判据：三层失败诚实出口全部走通=机制达标。
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "reports" / "e10" / "l1l2l3_e2e_results.json"
DB = ROOT / "data" / "test_agent.db"
TERMINAL = {"done", "failed", "cancelled"}

CELLS = [
    {"case": "E2E-1 主方法+接口对账",
     "path": r"D:\test program\go\go-rest-api-master（go_test1）",
     "requirement": "测所有 REST 接口",
     "expect": "endpoints>=6"},
    {"case": "E2E-2 L1 依赖闭包",
     "path": r"D:\test program\ts\nestjs-rest-sample-master（ts_test1）",
     "requirement": "测试用户模块的边界",
     "expect": "closure_msg + scope_skip_ledger"},
    {"case": "E2E-3 路由诚实出口",
     "path": r"D:\test agent_docker\data\demo\js-calc",
     "requirement": "测试支付模块的非法输入",
     "expect": "fallback_msg 或 [index·L2]"},
]


def wait_fire(hhmm: str) -> None:
    target = time.strptime(time.strftime("%Y-%m-%d") + " " + hhmm, "%Y-%m-%d %H:%M")
    wait = time.mktime(target) - time.time()
    if wait > 0:
        print(f"[e2e] 等待 {wait/3600:.1f}h 至 {hhmm} 低价窗（bugverify 批错峰）", flush=True)
        time.sleep(wait)


def task_messages(tid: int) -> list[str]:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ses = conn.execute(
            "SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        if not ses:
            return []
        rows = conn.execute(
            "SELECT content FROM agent_messages WHERE session_id=? ORDER BY seq",
            (ses["id"],)).fetchall()
        return [r["content"] for r in rows]
    finally:
        conn.close()


def judge(case: dict, tid: int, status: str) -> dict:
    msgs = " || ".join(task_messages(tid))
    ev = {"case": case["case"], "tid": tid, "status": status, "verdict": "FAIL",
          "evidence": ""}
    if case["case"].startswith("E2E-1"):
        m = re.search(r"\[analyze·index\] 需求命中接口清单——共 (\d+) 端点", msgs)
        n = int(m.group(1)) if m else 0
        ev["endpoints"] = n
        if m and n >= 6:
            ev["verdict"] = "PASS"
            ev["evidence"] = f"接口对账消息命中，count={n}"
        else:
            ev["evidence"] = f"接口对账消息缺失或 count<6（{m.group(0)[:60] if m else '无'}）"
    elif case["case"].startswith("E2E-2"):
        mc = re.search(r"需求命中模块 (\S+)（置信 \w+）\+依赖闭包 (\d+) 模块（L1）", msgs)
        skip = "索引路由范围外" in msgs
        ev["closure"] = mc.group(0) if mc else ""
        ev["scope_skip_ledger"] = skip
        if mc and skip:
            ev["verdict"] = "PASS"
            ev["evidence"] = f"闭包消息+scope 外跳过台账双证：{mc.group(0)}"
        else:
            ev["evidence"] = f"闭包消息={'有' if mc else '无'}；跳过台账={'有' if skip else '无'}"
    else:
        fb = "需求未命中索引——回退全库扫描" in msgs
        l2 = "[index·L2]" in msgs
        if fb or l2:
            ev["verdict"] = "PASS"
            ev["evidence"] = ("回退全库消息命中（诚实出口）" if fb
                              else "L2 急救消息命中（诚实出口）")
        else:
            ev["evidence"] = "既无回退全库消息也无 [index·L2] 消息"
    if status not in TERMINAL:
        ev["verdict"] = "FAIL"
        ev["evidence"] += "；终态异常：" + status
    return ev


def main() -> int:
    wait_fire("18:02")
    # 驱动器 v2（2026-11-19 遗憾清偿件 5）：点火前自检+响应码校验
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from driver_kit import create_and_run, ensure_docker, ensure_server
    token = ensure_server(BASE)
    ensure_docker()
    s = requests.Session()
    h = {"Authorization": "Bearer " + token}
    recs = []
    for i, cell in enumerate(CELLS):
        out = create_and_run(s, h, BASE, {"path": cell["path"],
                                          "requirement": cell["requirement"]})
        tid = out["tid"]
        recs.append({**cell, "tid": tid, "steps": out["steps"], "status": "queued"})
        print(f"[e2e {i+1}/{len(CELLS)}] {cell['case']} → #{tid} 入队（"
              + "、".join(f"{x['step']}={x['http']}" for x in out["steps"]) + "）", flush=True)
    # 串行等终态（最长 3h）
    t0, last = time.time(), ""
    while time.time() - t0 < 3 * 3600:
        states = []
        for rec in recs:
            d = s.get(f"{BASE}/api/tasks/{rec['tid']}", headers=h, timeout=10).json()
            rec["status"] = d.get("status")
            states.append(f"#{rec['tid']}:{rec['status']}")
        line = " ".join(states)
        if line != last:
            print(f"[{int((time.time()-t0)/60):>3}m] {line}", flush=True)
            last = line
        if all(r["status"] in TERMINAL for r in recs):
            break
        time.sleep(20)
    # 逐格判定（预注册判据）
    for rec in recs:
        rec.update(judge(rec, rec["tid"], rec.get("status", "")))
        print(f"[判定] {rec['case']} → {rec['verdict']}｜{rec['evidence'][:110]}", flush=True)
    passed = sum(1 for r in recs if r["verdict"] == "PASS")
    doc = {"generated_at": time.strftime("%F %T"),
           "verdict": f"{passed}/{len(recs)} PASS",
           "mechanism_ok": passed == len(recs),
           "cells": recs}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[e2e] 总判据：三层诚实出口 {passed}/{len(recs)} → {OUT}", flush=True)
    return 0 if passed == len(recs) else 1


if __name__ == "__main__":
    sys.exit(main())
