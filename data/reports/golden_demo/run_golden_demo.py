# -*- coding: utf-8 -*-
"""黄金演示驱动器（学院达标 T7，2026-11-25）。

--dry  夹具库零 LLM：按 docs/黄金演示脚本-学院验收.md 的九步+三负例走全流程
       （Playwright 断言 + API 断言），留档 run export / 步骤表 / 四表快照。
--live 真跑（T10-1）：须生产 server 双旗标重启 + 低价窗 + 预算停车线（总入口
       §预算规则）。本驱动器当前只实现 --dry；--live 在 T10-1 点火时接线。

留档：data/reports/golden_demo/dry_<ts>/
  run_1_replay.json（导出件同源数据）、steps_task1.csv、four_tables.json
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import test_agent.config as config  # noqa: E402
from test_agent.skills.prompt_library import AGENT_LOOP_PROMPT_VERSION  # noqa: E402

HERE = Path(__file__).resolve().parent
TMP_DB = HERE / "golden_dry.db"
PORT = 8770
BASE = f"http://127.0.0.1:{PORT}"

CLAMP_REQ = ("针对 js-calc 计算器：验证 clamp 边界钳制语义。clamp(v, lo, hi) 应为闭区间"
             "——v=hi 时应返回 hi 而不是 lo；当前实现对上边界处理可疑（10 被钳成 9）。"
             "请覆盖：正常值、下边界、上边界、边界外、非数值输入。")


def seed() -> None:
    """夹具库：任务1=已确认+跑完（clamp 检出，含 run/步骤/报告）；任务2=未确认（负例 N2）。"""
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    config.RUN_LEDGER_ENABLED = True
    config.AGENT_LOOP_ENABLED = True
    from test_agent.store import db
    db.init_db(TMP_DB)
    from test_agent.store.db import get_conn
    from test_agent.skills import run_ledger
    from test_agent.api.auth import ensure_default_users, hash_password
    c = get_conn(TMP_DB)
    ensure_default_users(c)          # admin/dev/caller 演示账号（真 hash，登录用）
    dev = c.execute("SELECT id FROM users WHERE username='dev'").fetchone()[0]
    caller = c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'caller')",
                       ("callerdemo", hash_password("caller123"))).lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'demo/js-calc','js-calc')",
                    (dev,)).lastrowid
    t1 = c.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "requirement_text,confirmed_at,created_at) VALUES(?,?,0.7,'failed',1,?,"
        "datetime('now'),datetime('now'))", (pid, caller, CLAMP_REQ)).lastrowid
    t2 = c.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "requirement_text,created_at) VALUES(?,?,0.7,'ready_for_review',0,?,datetime('now'))",
        (pid, caller, CLAMP_REQ)).lastrowid
    # 任务1 的运行账本（strategy 三步：读源码→预算耗尽拒→finish 带 strategy）
    sid, rid = run_ledger.start_run(c, t1, kind="single", goal_text=CLAMP_REQ,
                                    prompt_version=AGENT_LOOP_PROMPT_VERSION)
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=1,
                           action_type="call", capability="test.observe.read",
                           args={"path": "src/calc.js", "mode": "read"},
                           decision_json='{"action":"call","capability":"test.observe.read"}',
                           allowed=1, result_status="ok", result_summary="calc.js: 行 1-40",
                           decision_ms=1500, exec_ms=30)
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=2,
                           action_type="call", capability="test.run.unit",
                           # B1（F2 修复，2026-11-27）：args 改真实键——schema
                           # （capabilities.py test.run.unit）={targets 必填, timeout_s}；
                           # 旧 {"container_name","cmd"} 非合法参数形态（演示数据不实）
                           args={"targets": ["test_gen_r1.test.js"], "timeout_s": 120},
                           decision_json='{"action":"call","capability":"test.run.unit"}',
                           allowed=0, deny_reason="验证冻结任务仅允许 R0（test.run.unit=R2）",
                           decision_ms=900)
    run_ledger.record_step(c, rid, mode="strategy", loop_no=1, step_no=3,
                           action_type="finish",
                           decision_json='{"action":"finish","summary":"clamp 上边界可疑，'
                                         '生成边界用例","strategy":{"priorities":[{"order":1,'
                                         '"capability_id":"test.gen.cases","why":"证据足",'
                                         '"args_hint":{"feedback":"clamp 上边界未覆盖，补边界用例"}}]}}',
                           allowed=1, decision_ms=1100)
    run_ledger.finish_run(c, rid, status="failed",
                          stop_reason="疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单",
                          summary_json=json.dumps({
                              "report": [{"passed": 6, "failed": 1, "skipped": 0,
                                          "coverage_line": 0.86,
                                          "defects": json.dumps(
                                              {"failure_kind": "product_defect",
                                               "stop_reason": "疑似产品缺陷：clamp 上边界"})}],
                              "case_counts": {"passed": 6, "failed": 1, "error": 0,
                                              "skipped": 0, "total": 7},
                              "final": {"status": "failed",
                                        "error": "疑似产品缺陷：clamp 上边界"},
                              "loop_msgs": ["[strategy] 决策循环 goal_met（3 步）"],
                          }, ensure_ascii=False))
    # 报告行 + 用例结果（clamp 失败证人）
    c.execute("INSERT INTO test_reports(task_id, module_id, passed, failed, skipped,"
              " coverage_line, defects, per_file, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
              (t1, None, 6, 1, 0, 0.86,
               json.dumps({"failure_kind": "product_defect",
                           "stop_reason": "疑似产品缺陷：clamp 上边界"}, ensure_ascii=False),
               "[]", 24500))
    for i, (name, st) in enumerate((("clamp_上边界_10应得9证人", "failed"),
                                    ("add_正常", "passed"))):
        c.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name,"
                  " status, origin) VALUES (?,?,?,?,?,?, 'generated')",
                  (t1, 1, "L1", f"test_gen_r1.test.js#{i}", name, st))
    c.commit()
    c.close()


def start_server() -> bool:
    import uvicorn
    from test_agent.store.queue import QueueWorker
    QueueWorker.start = lambda self: None
    from test_agent.api.server import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                        log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/", timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def api_token(user: str, pwd: str) -> str:
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request(BASE + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    if args.live:
        return main_live()
    if not args.dry:
        print("[golden] 用 --dry（夹具库零 LLM）；--live 归 T10-1。")
        return 2

    # 前置自检（G6 教训）
    calc = ROOT / "data" / "demo" / "js-calc" / "src" / "calc.js"
    assert calc.exists() and "clamp" in calc.read_text(encoding="utf-8"), "calc.js 缺 clamp"
    seed()
    assert start_server(), "server 未起"
    dev_token = api_token("dev", "dev123")
    caller_token = api_token("callerdemo", "caller123")

    outdir = HERE / f"dry_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[bool, str]] = []

    def check(ok, name):
        results.append((bool(ok), name))
        print(("PASS  " if ok else "FAIL  ") + name, flush=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        js_errors: list[str] = []
        pg.on("pageerror", lambda e: js_errors.append(str(e)))

        # 步骤 1-3：caller 建任务（夹具已按此形态落库）——页面核验任务列表与角色语义
        pg.goto(f"{BASE}/")
        pg.evaluate(f"localStorage.setItem('token', '{caller_token}')")
        pg.reload()
        pg.goto(f"{BASE}/#/tasks")
        pg.wait_for_timeout(1200)
        check("clamp" in pg.content(), "步骤1 需求文本在任务列表可见（caller 建任务）")
        # N1：caller 直接下发 → 403
        try:
            req = urllib.request.Request(
                f"{BASE}/api/tasks/1/run", data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + caller_token})
            urllib.request.urlopen(req)
            check(False, "N1 caller 下发应 403")
        except urllib.error.HTTPError as e:
            check(e.code == 403, f"N1 caller 下发 403（实得 {e.code}）")
        # N2：未确认任务 developer 下发 → 409
        try:
            req = urllib.request.Request(
                f"{BASE}/api/tasks/2/run", data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + dev_token})
            urllib.request.urlopen(req)
            check(False, "N2 未确认下发应 409")
        except urllib.error.HTTPError as e:
            check(e.code == 409, f"N2 未确认下发 409（实得 {e.code}）")

        # 步骤 4：Agent 运行中心决策时间线（strategy 三步）
        pg.evaluate(f"localStorage.setItem('token', '{dev_token}')")
        pg.goto(f"{BASE}/#/run/1")
        pg.wait_for_selector("#rd-hero h3", timeout=8000)
        tl = pg.inner_text("#rd-timeline-body")
        check("🔧" in tl and "test.observe.read" in tl, "步骤4 时间线读证据调用")
        check("⛔" in tl and "仅允许 R0" in tl, "步骤4 时间线策略闸拒绝红色")
        check("🏁" in tl and "strategy" in tl, "步骤4 时间线 finish 带 strategy")

        # 步骤 7：报告页 clamp 检出
        pg.goto(f"{BASE}/#/task/1")
        pg.wait_for_timeout(1500)
        content = pg.content()
        check("clamp" in content, "步骤7 报告页含 clamp 用例/需求")

        # 步骤 8：运行中心四指标
        check("工具调用成功率" in pg_wait(pg), "步骤8 详情头部四指标")

        # 步骤 9：导出回放 JSON
        pg.goto(f"{BASE}/#/run/1")
        pg.wait_for_selector("#rd-export", timeout=8000)
        pg.click("#rd-export")
        pg.wait_for_timeout(800)
        check("已下载" in pg.inner_text("#rd-export-hint"), "步骤9 导出回放 JSON")

        # N3：Skill 级拒绝审计行（skill_demo 同款：停用拒）
        try:
            from test_agent.skills.registry import SkillRegistry, Skill
            from test_agent.store.db import get_conn
            c = get_conn(TMP_DB)
            reg = SkillRegistry(conn=c)
            skill = Skill(name="demo_skill", description="负例", func=lambda t, a: "x",
                          risk_level="low", timeout_ms=1000)
            reg.register(skill)
            c.execute("UPDATE skills SET enabled=0 WHERE name='demo_skill'")
            c.commit()
            r = reg.invoke(1, skill, {})
            check(r.status == "denied" and r.invocation_id > 0,
                  "N3 Skill 级停用拒绝落审计")
            c.close()
        except Exception as e:      # noqa: BLE001
            check(False, f"N3 异常 {type(e).__name__}: {e}")

        check(not js_errors, f"全程零未捕获 JS 异常（实得 {len(js_errors)}）")
        b.close()

    # 留档：run export / 步骤表 / 四表快照
    conn_u = urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/api/runs/1/export",
                               headers={"Authorization": "Bearer " + dev_token}))
    (outdir / "run_1_replay.json").write_bytes(conn_u.read())
    from test_agent.store.db import get_conn
    c = get_conn(TMP_DB)
    steps = c.execute("SELECT run_id, mode, loop_no, step_no, action_type, capability,"
                      " allowed, deny_reason, result_status FROM agent_steps ORDER BY id"
                      ).fetchall()
    cols = steps[0].keys() if steps else []
    with open(outdir / "steps_task1.csv", "w", encoding="utf-8") as fh:
        fh.write(",".join(cols) + "\n")
        for s in steps:
            fh.write(",".join(json.dumps(str(s[k]), ensure_ascii=False) for k in cols) + "\n")
    four = {}
    for t in ("agent_runs", "agent_steps", "llm_calls", "skill_invocations"):
        four[t] = c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
    (outdir / "four_tables.json").write_text(
        json.dumps(four, ensure_ascii=False, indent=2), encoding="utf-8")
    c.close()
    # 四表快照：dry 无 LLM → llm_calls=0 属诚实（契约：没数据不造数）；三表必须非零
    check(all(four[t] > 0 for t in ("agent_runs", "agent_steps", "skill_invocations"))
          and four["llm_calls"] == 0, f"四表快照（dry：llm_calls=0 属诚实）{four}")

    ok = all(o for o, _ in results)
    (outdir / "results.json").write_text(
        json.dumps({"pass": sum(1 for o, _ in results if o), "total": len(results),
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"({sum(1 for o, _ in results if o)}/{len(results)}) | 留档 → {outdir}")
    return 0 if ok else 1


def pg_wait(pg) -> str:
    pg.goto(f"{BASE}/#/run/1")
    pg.wait_for_selector("#rd-hero h3", timeout=8000)
    return pg.inner_text("#rd-hero")


# ---------------- --live（B6 接线，2026-11-27；T7 只实现了 --dry） ----------------
# 真跑：连生产 server（127.0.0.1:8000，须 MTA_AGENT_LOOP=1 MTA_RUN_LEDGER=1 双旗标
# 已启动）→ caller 建真实 js-calc 任务 → dev 确认+下发 → 等终态 → 对真实 run 断言
# （时间线/报告页/导出/四表齐）。预算闸：单任务 chat 价估算 ≤¥0.15（B6 停车线）；
# 高峰段闸拒绝点火（00:30-09:00/12-14/18-次日00:30 之外不跑）。留档 live_<ts>/。
LIVE_BASE = "http://127.0.0.1:8000"
LIVE_PARKING = 0.15


def _live_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(LIVE_BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _in_low_price_window() -> bool:
    """低价窗=00:30-09:00、12:00-14:00、18:00-次日00:30（用户 2026-11-11 硬规则）。"""
    m = time.strftime("%H:%M")
    return ("00:30" <= m < "09:00") or ("12:00" <= m < "14:00") or (m >= "18:00") or (m < "00:30")


def _task_cost_est(task_id: int) -> float:
    import sqlite3
    conn = sqlite3.connect("file:" + (config.DATA_DIR / "test_agent.db").as_posix()
                           + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT prompt_tokens, completion_tokens FROM llm_calls "
                            "WHERE task_id=?", (task_id,)).fetchall()
    finally:
        conn.close()
    return round(sum((pt or 0) / 1e6 + (cm or 0) / 2e6      # chat 价 (1,2)
                     for pt, cm in rows), 4)


def main_live() -> int:
    results: list[tuple[bool, str]] = []

    def check(ok, name):
        results.append((bool(ok), name))
        print(("PASS  " if ok else "FAIL  ") + name, flush=True)

    if not _in_low_price_window():
        print("[golden-live] 高峰段——拒绝点火（硬规则 2026-11-11）")
        return 2
    outdir = HERE / f"live_{time.strftime('%Y%m%d-%H%M%S')}"

    def _login(user, pwd):
        body = json.dumps({"username": user, "password": pwd}).encode()
        req = urllib.request.Request(LIVE_BASE + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]

    try:
        dev_token = _login("dev", "dev123")
        caller_token = _login("caller", "caller123")
        ledger = _live_api("GET", "/api/runs?limit=1", dev_token)
        check(bool(ledger.get("ledger_enabled")), "生产 server 账本开（双旗标）")
    except Exception as e:      # noqa: BLE001
        print(f"[golden-live] 生产 server 不可达或未双旗标启动：{e}")
        return 2

    calc = ROOT / "data" / "demo" / "js-calc" / "src" / "calc.js"
    check(calc.exists() and "clamp" in calc.read_text(encoding="utf-8"),
          "被测仓 js-calc clamp 就位")

    # 真任务：幂等——已有终态 golden-live 任务则复用（B6 重跑不烧新格）；无则建新
    import sqlite3
    _db = config.DATA_DIR / "test_agent.db"
    _ro = sqlite3.connect("file:" + _db.as_posix() + "?mode=ro", uri=True)
    _ro.row_factory = sqlite3.Row
    prev = _ro.execute("SELECT id, status FROM test_tasks "
                       "WHERE requirement_text=? AND status IN ('done','failed','cancelled') "
                       "ORDER BY id DESC LIMIT 1", (CLAMP_REQ,)).fetchone()
    tid = prev["id"] if prev else None
    row = {"status": prev["status"]} if prev else {}
    if tid is None:
        t = _live_api("POST", "/api/tasks", caller_token,
                      {"repo_url": str(ROOT / "data/demo/js-calc"), "requirement": CLAMP_REQ,
                       "coverage_target": 0.7, "name": f"golden-live-{time.strftime('%H%M%S')}"})
        tid = t["task_id"]
        _live_api("POST", f"/api/tasks/{tid}/confirm", dev_token)
        _live_api("POST", f"/api/tasks/{tid}/run", dev_token)
        t0 = time.time()
        row = {}
        while time.time() - t0 < 900:
            row = _live_api("GET", f"/api/tasks/{tid}", dev_token)
            if row.get("status") in ("done", "failed", "cancelled"):
                break
            time.sleep(6)
    else:
        print(f"[golden-live] 复用既有终态任务 tid={tid}（幂等重跑，零新花费）")
    _ro.close()
    check(row.get("status") in ("done", "failed"), f"真任务终态（tid={tid} {row.get('status')}）")
    cost = _task_cost_est(tid)
    check(cost <= LIVE_PARKING, f"停车线内（chat 价估算 ¥{cost} ≤ ¥{LIVE_PARKING}）")

    # 负例（真库语义）：N1 caller 下发 403；N2 未确认任务 409
    try:
        _live_api("POST", f"/api/tasks/{tid}/run", caller_token)
        check(False, "N1 caller 下发应 403")
    except urllib.error.HTTPError as e:
        check(e.code == 403, f"N1 caller 下发 403（实得 {e.code}）")
    _ro2 = sqlite3.connect("file:" + _db.as_posix() + "?mode=ro", uri=True)
    prev_n2 = _ro2.execute("SELECT id FROM test_tasks WHERE requirement_text=? "
                           "AND confirmed=0 ORDER BY id DESC LIMIT 1",
                           ("N2 未确认负例",)).fetchone()
    _ro2.close()
    if prev_n2 is None:
        t2 = _live_api("POST", "/api/tasks", caller_token,
                       {"repo_url": str(ROOT / "data/demo/js-calc"),
                        "requirement": "N2 未确认负例",
                        "coverage_target": 0.7,
                        "name": f"golden-live-n2-{time.strftime('%H%M%S')}"})
        n2id = t2["task_id"]
    else:
        n2id = prev_n2[0]
    try:
        _live_api("POST", f"/api/tasks/{n2id}/run", dev_token)
        check(False, "N2 未确认下发应 409")
    except urllib.error.HTTPError as e:
        check(e.code == 409, f"N2 未确认下发 409（实得 {e.code}）")

    # 对真实 run 的 UI 断言（时间线含真实步/报告页/导出）
    runs = _live_api("GET", f"/api/runs?task_id={tid}&limit=5", dev_token)
    items = runs.get("runs") or runs.get("items") or (runs if isinstance(runs, list) else [])
    check(bool(items), f"run 行落账（n={len(items)}）")
    rid = (items[0] or {}).get("id") if items else None
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        # 深链前先落地首页+置 token+整页 reload（同文档 hash 跳转不触发路由——
        # dry 版靠 reload 触发；B6 首跑实锤：不 reload 则 section.page 保持 hidden）
        pg.goto(f"{LIVE_BASE}/")
        pg.evaluate(f"localStorage.setItem('token', '{dev_token}')")
        pg.reload()
        pg.wait_for_timeout(800)
        pg.goto(f"{LIVE_BASE}/#/run/{rid}")
        pg.wait_for_selector("#rd-hero h3", timeout=8000)
        tl = pg.inner_text("#rd-timeline-body") if pg.query_selector("#rd-timeline-body") else ""
        check("finish" in tl or "🏁" in tl, "时间线含 finish（真模型决策）")
        pg.goto(f"{LIVE_BASE}/#/task/{tid}")
        pg.reload()
        pg.wait_for_timeout(1500)
        check("clamp" in pg.content(), "报告页含 clamp 用例/需求")
        pg.goto(f"{LIVE_BASE}/#/run/{rid}")
        pg.reload()
        pg.wait_for_selector("#rd-export", timeout=8000, state="visible")
        pg.click("#rd-export")
        pg.wait_for_timeout(800)
        hint = pg.inner_text("#rd-export-hint") if pg.query_selector("#rd-export-hint") else ""
        check("已下载" in hint, "导出回放 JSON")
        b.close()

    # 四表齐（真库直读）
    import sqlite3
    conn = sqlite3.connect("file:" + (config.DATA_DIR / "test_agent.db").as_posix()
                           + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    four = {}
    for tb in ("agent_runs", "agent_steps", "llm_calls", "skill_invocations"):
        four[tb] = conn.execute(f"SELECT COUNT(*) AS n FROM {tb} WHERE task_id=?",
                                (tid,)).fetchone()["n"] if tb == "llm_calls" else \
            conn.execute(f"SELECT COUNT(*) AS n FROM {tb}").fetchone()["n"]
    conn.close()
    (outdir).mkdir(parents=True, exist_ok=True)
    (outdir / "four_tables.json").write_text(json.dumps(four, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    check(all(four[t] > 0 for t in ("agent_runs", "agent_steps", "llm_calls")),
          f"四表齐（live：llm_calls>0 属真实）{four}")

    ok = all(o for o, _ in results)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "results.json").write_text(
        json.dumps({"task_id": tid, "cost_yuan_est": cost,
                    "pass": sum(1 for o, _ in results if o), "total": len(results),
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"({sum(1 for o, _ in results if o)}/{len(results)}) | 留档 → {outdir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
