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
                           args={"container_name": "mta-run-1", "cmd": ["npx", "jest"]},
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
        print("[golden] --live 归 T10-1：生产 server 双旗标重启+低价窗+预算停车线（总入口）。"
              "本驱动器当前只实现 --dry。")
        return 2
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


if __name__ == "__main__":
    sys.exit(main())
