# -*- coding: utf-8 -*-
"""诊断夹具驱动器（任务四 AL-7，2026-11-26 测试任务报告 §4.2）——已知答案测 diagnose 节点。

做法（设计原文）：建两阶段任务（js-calc，需求用基线 #1156 原文）→ confirm → 等阶段一
停在待审查 → 用接口把 AI 出的用例全部停用 → 植入夹具用例 → 放行阶段二 → 等终态 → 读账本。

路由自检：每格先确认第一轮判定确实是补测、账本里确实有诊断循环；两条不满足，
该格记"夹具未触发"，不进分母也不算失败。D1 未触发诊断允许同类调形态 2 次。

用法：
  python -I run_fixtures.py --classes D1,D2,D3 --reps 3 --parking 3.4
  python -I run_fixtures.py --classes D1 --dry
标签：data/reports/agentloop_scale/fixture_labels.json（{task_id: {class, confidence}}）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
PROD_DB = ROOT / "data" / "test_agent.db"
HERE = Path(__file__).parent
CELLS = HERE / "cells"
LABELS = HERE / "fixture_labels.json"
PRICE = {"deepseek-chat": (1, 2)}
PEAK_START = ("09:00", "14:00")
REPO = ROOT / "data/demo/L2场景-coupon-api"
BASELINE = 1155

# 夹具用例（Python/pytest 形态，coupon-api=天然干净项目——阶段二结构性地会重新生成
# AI 用例（2637/2638 实锤：删文件无效），干净项目上 AI 用例全过=诊断时失败只剩夹具
# =纯信号；js-calc 上做会混入 clamp 真缺陷失败 → 判据失真。此为 P0 现场设计修订，
# 依应变协议留痕：夹具宿主 js-calc→coupon-api，类别与判据不变）
FIXTURES = {
    "D1": {
        "class": "scaffold_error",
        "confidence": "high",
        "code": (
            "import os, sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
            "from coupon_nope import nothing\n"
            "\n"
            "def test_d1_scaffold_probe():\n"
            "    assert True"
        ),
    },
    "D2": {
        "class": "expectation_dispute",
        "confidence": "high",
        # 【协议修订 2026-11-27，任务卡 A3】原 fastapi TestClient 形态三格全被收集闸
        # 废案（收集闸先于依赖安装，import fastapi 在收集期崩=夹具未达诊断层）。
        # 改纯 stdlib 形态过收集闸：断言恒假 + expected_basis 常识 → 诊断层应判
        # expectation_dispute。宿主维持 coupon-api（P0 修订留痕不变）。
        "code": (
            "def test_d2_probe():\n"
            "    assert 1 == 2  # expected_basis: 常识"
        ),
    },
    "D3": {
        "class": "scaffold_error",       # 收集失败形态（零可收集用例）
        "confidence": "medium",
        "code": (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason=\"zero-exec fixture\")\n"
            "def test_d3_skip_probe():\n"
            "    assert True"
        ),
    },
}


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e


def load_req() -> str:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                            (BASELINE,)).fetchone()[0]
    finally:
        conn.close()


def cost_of_task(tid: int) -> float:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT model, prompt_tokens, completion_tokens FROM llm_calls "
                            "WHERE task_id=?", (tid,)).fetchall()
    finally:
        conn.close()
    return round(sum((p or 0) / 1e6 + (cm or 0) / 1e6 * 2
                     for _m, p, cm in rows), 4)


def spent_so_far(tag: str = "") -> float:
    """tag 非空=只算该批次（结算件名 fixture_<tag>_*）；tag 空=只算无 tag 批
    （fixture_D* 旧命名——不能把别的批次的账算进来）。"""
    import re as _re
    total = 0.0
    if tag:
        files = CELLS.glob(f"fixture_{tag}_*.json")
    else:
        files = [f for f in CELLS.glob("fixture_*.json")
                 if not _re.match(r"fixture_[a-z]", f.name)]
    for f in files:
        total += json.load(open(f, encoding="utf-8")).get("cost_yuan_est", 0.0)
    return round(total, 4)


def in_peak() -> bool:
    now = time.strftime("%H:%M")
    return PEAK_START[0] <= now < PEAK_START[1]


def wait_terminal(tid: int, token: str, want: set[str], timeout_s: int) -> str:
    t0 = time.time()
    row = {}
    while time.time() - t0 < timeout_s:
        row = api("GET", f"/api/tasks/{tid}", token)
        if row.get("status") in want:
            return row["status"]
        time.sleep(5)
    return row.get("status") or "timeout"


def disable_all_ai_cases(tid: int, token: str) -> int:
    cases = api("GET", f"/api/tasks/{tid}/cases", token)
    items = cases.get("cases") or cases.get("items") or cases
    n = 0
    for c in items if isinstance(items, list) else []:
        if c.get("enabled", 1) and not str(c.get("name", "")).startswith("test_d"):
            api("POST", f"/api/cases/{c['id']}/toggle", token, {})
            n += 1
    return n


def diagnose_steps_n(tid: int) -> int:
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT s.id FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id "
            "WHERE r.task_id=? AND s.mode='diagnose'", (tid,)).fetchall()
        return len(rows)
    finally:
        conn.close()


def run_fixture(cls: str, rep: int, dev_token: str, caller_token: str,
                tag: str = "") -> dict:
    fx = FIXTURES[cls]
    req = load_req()
    # 两阶段任务（设计 §4.2）：two_stage=true；确认=授权+自动启动阶段一
    t = api("POST", "/api/tasks", caller_token,
            {"repo_url": str(REPO), "requirement": req,
             "coverage_target": 0.7, "name": f"al-fx-{cls}-{rep}" + (f"-{tag}" if tag else ""),
             "two_stage": True})
    tid = t["task_id"]
    api("POST", f"/api/tasks/{tid}/confirm", dev_token)
    fname = f"fixture_{tag + '_' if tag else ''}{cls}_{rep}.json"
    # 阶段一：停在待审查（ready_for_review）；20 分钟封顶
    st = wait_terminal(tid, dev_token, {"ready_for_review", "failed", "done",
                                        "cancelled"}, 1200)
    notes = {"stage1_status": st}
    if st != "ready_for_review":
        row = api("GET", f"/api/tasks/{tid}", dev_token)
        rec = {"cls": cls, "rep": rep, "tag": tag, "task_id": tid, "status": st,
               "triggered": False,
               "error": (row.get("error") or "")[:200],
               "cost_yuan_est": cost_of_task(tid), "notes": notes}
        (CELLS / fname).write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[fx] {cls}#{rep} 阶段一未停等（{st}）——夹具未触发，tid={tid}", flush=True)
        return rec
    # 停用 AI 用例（DB 侧留痕）+ 删除工作区 AI 测试文件（执行侧纯化——阶段二跑的
    # 是工作区文件，DB 停用不阻止执行；2637 实锤：不删文件则 repair 前的执行仍带
    # AI 用例，真实缺陷失败混入夹具信号 → 诊断判 product_defect）
    notes["disabled_ai_cases"] = disable_all_ai_cases(tid, dev_token)
    ws = ROOT / "data" / "workspaces" / f"task_{tid}"
    removed = 0
    if ws.exists():
        for pat in ("test_gen_r*.py", "test_gen_r*.js", "test_gen_fb*.py",
                    "test_imported*.py", "test_imported*.js"):
            for p in ws.glob(pat):
                p.unlink(missing_ok=True)
                removed += 1
    notes["removed_workspace_files"] = removed
    api("POST", f"/api/tasks/{tid}/cases", dev_token,
        {"name": f"test_d{cls[1:]}_probe_{rep}", "code": fx["code"],
         "method": "夹具植入"})
    api("POST", f"/api/tasks/{tid}/run", dev_token)
    st2 = wait_terminal(tid, dev_token, {"done", "failed", "cancelled"}, 1800)
    diag_n = diagnose_steps_n(tid)
    triggered = diag_n > 0        # 路由自检②：账本里有诊断循环
    # 路由自检①：第一轮判定是补测（repair 轮存在）——由诊断循环存在近似（同账本）
    rec = {"cls": cls, "rep": rep, "tag": tag, "task_id": tid, "status": st2,
           "triggered": triggered, "diagnose_steps": diag_n,
           "cost_yuan_est": cost_of_task(tid), "notes": notes}
    (CELLS / fname).write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    # 标签落档（kpi 脚本消费）；tag 区分批次（如 P1b 三闸重跑=gates）
    labels = json.loads(LABELS.read_text(encoding="utf-8")) if LABELS.exists() else {}
    labels[str(tid)] = {"class": fx["class"], "confidence": fx["confidence"],
                        "fixture": cls, "triggered": triggered,
                        **({"tag": tag} if tag else {})}
    LABELS.write_text(json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[fx] {cls}#{rep} tid={tid} status={st2} 诊断循环={diag_n} "
          f"{'触发' if triggered else '未触发'} cost≈¥{rec['cost_yuan_est']}", flush=True)
    return rec


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--classes", default="D1,D2,D3")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--parking", type=float, required=True)
    ap.add_argument("--tag", default="", help="批次标签（结算件 fixture_<tag>_*；防覆盖）")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    CELLS.mkdir(parents=True, exist_ok=True)
    classes = [c.strip().upper() for c in args.classes.split(",") if c.strip()]
    plan = [(c, i) for c in classes for i in range(1, args.reps + 1)]
    print(f"[fixtures] 计划 {len(plan)} 格（{classes}×{args.reps} tag={args.tag or '无'}）；"
          f"已花费 ¥{spent_so_far(args.tag)}；停车线 ¥{args.parking}")
    if args.dry:
        for c, i in plan:
            print(f"  {c}#{i} 类别={FIXTURES[c]['class']} 置信={FIXTURES[c]['confidence']}"
                  f" 步骤=两阶段→停用AI用例→植入夹具→放行→读账本")
        print("[fixtures] dry 完成——未烧钱")
        return 0

    def _login(user: str, pwd: str) -> str:
        body = json.dumps({"username": user, "password": pwd}).encode()
        req = urllib.request.Request(BASE + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]

    dev_token = _login("dev", "dev123")
    caller_token = _login("caller", "caller123")
    form_attempt = {c: 0 for c in classes}      # D1 调形态计数
    for cls, rep in plan:
        out_name = f"fixture_{args.tag + '_' if args.tag else ''}{cls}_{rep}.json"
        if (CELLS / out_name).exists():
            print(f"[skip] {out_name} 已留档")
            continue
        if in_peak():
            print(f"[gate] 高峰段——停新格")
            break
        if spent_so_far(args.tag) + 0.08 > args.parking:
            print(f"[gate] 触停车线 ¥{spent_so_far(args.tag)}——停止放量")
            break
        rec = run_fixture(cls, rep, dev_token, caller_token, tag=args.tag)
        if cls == "D1" and not rec.get("triggered") and form_attempt[cls] < 2:
            # 调形态：D1 未触发诊断允许同类调 2 次（设计 §4.2）
            form_attempt[cls] += 1
            print(f"[fx] D1 未触发——调形态第 {form_attempt[cls]} 次重跑该格")
            run_fixture(cls, rep, dev_token, caller_token, tag=args.tag)
    print(f"[fixtures] 累计花费 ¥{spent_so_far(args.tag)} / 停车线 ¥{args.parking}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
