# -*- coding: utf-8 -*-
"""六语言对照实验（2026-11-18 深夜，用户令"所有语言所有项目各跑一遍+与基线对比"）。

设计：六语言 demo 各 1 格，需求**逐字复用** 2026-11-15 六语言真机验收基线
（任务 1155-1160）文本，配置同基线（单阶段+代审 off）——唯一变量=平台代码
（L1/L2/L3 路由生效）。跑完自动拉双侧数据出对比表：
  token 用量 / 终态判定（done=成功，真检出 stop+证人=正确行为）/ 覆盖率 /
  用例数 / 路由消息命中（哪层触发）/ scope 跳过台账数。

基线（llm_calls 实测）：
  py#1155 14416 done 0.99｜js#1156 16079 真检出 1.0｜ts#1157 16613 真检出 1.0
  java#1158 29280 stop（服务编排修复前失败形态）｜go#1159 14640 真检出 0.79
  php#1160 13605 0.02（黑盒本质）——合计 104,633 tok
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "reports" / "l1l2l3_real" / "matrix_results.json"
DB = ROOT / "data/test_agent.db"
TERMINAL = {"done", "failed", "cancelled"}

CELLS = [
    {"lang": "python", "path": str(ROOT / "data/demo/L2场景-coupon-api"),
     "baseline_tid": 1155,
     "requirement": "优惠券 API 需求（L2 冒烟演示）\n1. 创建优惠券：code 长度 2~8、percent 1~100（整数），成功返回 201 与完整字段\n2. 按 id 查询优惠券：存在返回 200，不存在返回 404\n3. 下单使用优惠券：金额满 min_total 才可用，折扣（字段 discount）=amount*percent/100 向下取整，应付（字段 total）=amount-折扣\n4. 同一优惠券重复下单 → 400"},
    {"lang": "javascript", "path": str(ROOT / "data/demo/js-calc"),
     "baseline_tid": 1156,
     "requirement": "验证 src/calc.js 的三个函数：\n1. add(a, b)：两数相加；任一参数非数字时抛 TypeError。\n2. div(a, b)：a 除以 b；b 为 0 时抛 RangeError。\n3. clamp(x, lo, hi)：把 x 限制在闭区间 [lo, hi]——x 小于 lo 返回 lo，x 大于 hi 返回 hi，x 等于 hi 时必须返回 hi 本身。"},
    {"lang": "typescript", "path": str(ROOT / "data/demo/ts-calc"),
     "baseline_tid": 1157,
     "requirement": "# 验收需求（ts-calc，TypeScript 执行链路·白包阶段1）\n\n被测模块 src/calc.ts，三组行为：\n\n1. add(a, b)：返回两数之和（等价类：正数/负数/零组合）\n2. clamp(v, lo, hi)：把 v 钳制到闭区间 [lo, hi]——v < lo 返回 lo；v > hi 返回 hi；\n   区间内（含 v==lo 与 v==hi 两端点）原样返回 v（边界值分析重点）\n3. isValidCode(code)：2~8 位字母数字串返回 true，否则 false"},
    {"lang": "java", "path": str(ROOT / "data/demo/java-api-book"),
     "baseline_tid": 1158,
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"lang": "go", "path": str(ROOT / "data/demo/go-api-book"),
     "baseline_tid": 1159,
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"lang": "php", "path": str(ROOT / "data/demo/php-api-book"),
     "baseline_tid": 1160,
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
]


def db_metrics(tid: int) -> dict:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        lc = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok "
                          "FROM llm_calls WHERE task_id=?", (tid,)).fetchone()
        ev = conn.execute("""SELECT coverage_line, verdict, failure_kind, stop_reason
                             FROM evaluation_rounds WHERE task_id=?
                             ORDER BY round_no DESC LIMIT 1""", (tid,)).fetchone()
        cases = conn.execute("""SELECT COUNT(*) n FROM test_cases c
                                JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=?""",
                             (tid,)).fetchone()
        ses = conn.execute("SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (tid,)).fetchone()
        msgs = [r["content"] for r in conn.execute(
            "SELECT content FROM agent_messages WHERE session_id=? ORDER BY seq",
            (ses["id"],))] if ses else []
        route = [m for m in msgs if "[analyze·index]" in m]
        l2 = [m for m in msgs if "[index·L2]" in m]
        skip = sum(m.count("（索引路由范围外）") for m in msgs) + \
            sum(1 for m in msgs if "索引路由范围外" in m and m.startswith("[generate"))
        gen_skipped = 0
        for m in msgs:
            if "source_skipped" in m or "索引路由范围外" in m:
                pass
        return {
            "tokens": lc["tok"], "llm_calls": lc["n"],
            "coverage": ev["coverage_line"] if ev else None,
            "verdict": ev["verdict"] if ev else None,
            "failure_kind": ev["failure_kind"] if ev else None,
            "stop_reason": (ev["stop_reason"] or "")[:80] if ev else "",
            "cases": cases["n"],
            "route_msg": route[-1][:150] if route else "",
            "route_hit": bool(route) and "未命中" not in route[-1],
            "l2_msgs": [m[:120] for m in l2],
        }
    finally:
        conn.close()


def baseline_metrics(tid: int) -> dict:
    m = db_metrics(tid)
    return {k: m[k] for k in ("tokens", "coverage", "verdict", "cases", "stop_reason")}


def main() -> int:
    s = requests.Session()
    h = {"Authorization": "Bearer " + s.post(
        f"{BASE}/api/login", json={"username": "admin", "password": "admin123"},
        timeout=10).json()["token"]}
    recs = []
    for i, cell in enumerate(CELLS):
        tid = s.post(f"{BASE}/api/tasks", headers=h, timeout=10, json={
            "repo_url": cell["path"], "requirement": cell["requirement"],
            "two_stage": False, "ai_reviewer": "off"}).json()["task_id"]
        s.post(f"{BASE}/api/tasks/{tid}/confirm", headers=h, timeout=10)
        r = s.post(f"{BASE}/api/tasks/{tid}/run", headers=h, timeout=15)
        rec = {**cell, "tid": tid, "run_http": r.status_code, "status": "queued",
               "baseline": baseline_metrics(cell["baseline_tid"])}
        recs.append(rec)
        print(f"[matrix {i+1}/6] [{cell['lang']}] → #{tid} 入队（基线 #{cell['baseline_tid']} "
              f"{rec['baseline']['tokens']} tok）", flush=True)
        time.sleep(3)   # 入队间隔，防 confirm 竞态

    t0, last = time.time(), ""
    while time.time() - t0 < 4 * 3600:
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
        time.sleep(30)

    # ---------- 拉数据出对比 ----------
    for rec in recs:
        rec["new"] = db_metrics(rec["tid"])
    new_tok = sum(r["new"]["tokens"] for r in recs)
    old_tok = sum(r["baseline"]["tokens"] for r in recs)
    summary = {
        "generated_at": time.strftime("%F %T"),
        "token_total": {"baseline": old_tok, "new": new_tok,
                        "delta_pct": round((new_tok - old_tok) / old_tok * 100, 1)},
        "cells": [{k: c.get(k) for k in ("lang", "tid", "baseline_tid", "status")}
                  | {"baseline": c["baseline"], "new": {k: c["new"][k] for k in
                     ("tokens", "coverage", "verdict", "cases", "route_hit", "route_msg",
                      "l2_msgs", "failure_kind", "stop_reason")}}
                  for c in recs],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[matrix] token 基线 {old_tok} → 新 {new_tok}（{summary['token_total']['delta_pct']:+.1f}%）",
          flush=True)
    for c in recs:
        n, b = c["new"], c["baseline"]
        print(f"  [{c['lang']:>10}] #{c['tid']} {c['status']:>7} tok {b['tokens']:>6}→{n['tokens']:>6} "
              f"覆盖 {b['coverage']}→{n['coverage']} 用例 {b['cases']}→{n['cases']} "
              f"路由={'命中' if n['route_hit'] else '未命中'}", flush=True)
    print(f"[matrix] 结果落 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
