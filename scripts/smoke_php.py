"""白包阶段4·PHP Tier2 真机验收冒烟（docs/多语言工具链接入任务包-20261106.md 阶段4）。

验收条目（任务卡【验收】）：PHPUnit demo 全链路——composer.json 探测→composer install
（phpunit 随项目走）→ 生成 TestGenR1.php（PHPUnit 类壳）→ php -l 语法闸 → phpunit 执行
（--log-junit+--coverage-clover pcov）→ php_adapter 统一文本 → 归因/报告正常
+ 埋一个真缺陷（getTier(90) 边界）被归因为 product_defect（证人如实上报）。

用法：python scripts/smoke_php.py（真 Docker + 真 LLM，tmp 库，不碰真实库/真实任务链）
证据：data/reports/php_accept/smoke_php_<ts>.json
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "php_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "php_accept"
DEMO = ROOT / "data" / "demo" / "php-shop"


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-php','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)", (uid, str(DEMO), "php-shop-demo", None)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()

    from test_agent.graph.builder import build_graph
    from test_agent.graph.nodes import make_deps
    deps = make_deps()
    conn2 = deps.conn
    lock = conn2.execute(
        """UPDATE test_tasks SET status='cloning', updated_at=datetime('now')
           WHERE id=? AND status NOT IN ('cloning','analyzing','provisioning',
                                         'generating','running','reporting')""", (tid,))
    conn2.commit()
    assert lock.rowcount == 1, "任务被锁（并发态）"
    deps.runner.cleanup(f"mta-run-{tid}")
    cur = conn2.execute(
        "INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')", (tid,))
    conn2.commit()
    init = {"task_id": tid, "session_id": cur.lastrowid, "repo_url": str(DEMO),
            "requirement": (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"),
            "container_name": f"mta-run-{tid}", "round_no": 0,
            "coverage_target": config.COVERAGE_TARGET, "fingerprints": [], "auto_prepare": False}
    graph = build_graph(deps)
    print(f"[smoke-php] 任务 {tid} L1 全图起跑（demo={DEMO.name}，真 Docker+真 LLM）…")
    t0 = time.time()
    try:
        final = graph.invoke(init, {"recursion_limit": 60})
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{tid}")
        except Exception as e:  # noqa: BLE001
            print("容器回收失败:", e)
    elapsed = round(time.time() - t0, 1)

    c = get_conn()
    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    gen_files = sorted(p.name for p in ws.glob("TestGen*.php"))
    scripts = [dict(r) for r in c.execute(
        "SELECT framework, filename, version FROM test_scripts WHERE task_id=?", (tid,))]
    rounds = [dict(r) for r in c.execute(
        "SELECT round_no, passed, failed, verdict FROM evaluation_rounds "
        "WHERE task_id=? ORDER BY round_no", (tid,))]
    cases = [dict(r) for r in c.execute(
        "SELECT round_no, nodeid, status, origin FROM test_case_results "
        "WHERE task_id=? ORDER BY id DESC LIMIT 20", (tid,))]
    llm = [dict(r) for r in c.execute(
        "SELECT phase, model, total_tokens FROM llm_calls WHERE task_id=?", (tid,))]
    tokens = sum(x["total_tokens"] or 0 for x in llm)
    lang_row = c.execute(
        "SELECT primary_language FROM projects WHERE id=?", (pid,)).fetchone()

    checks = {
        "语言判定=php": lang_row["primary_language"] == "php",
        "生成了 TestGen*.php（项目根）": bool(gen_files),
        "framework=phpunit 落库": all(s["framework"] == "phpunit" for s in scripts) and bool(scripts),
        "编译闸+执行轮次落库（终态可判）": bool(rounds),
        "逐用例结果落库（phpunit nodeid）": any("::" in (x["nodeid"] or "") for x in cases),
        "埋的 getTier(90) 边界缺陷被暴露（product_defect 证人）": any(
            x["status"] == "failed" for x in cases) or any(r["failed"] > 0 for r in rounds),
        "终态产出": bool(final.get("verdict")),
    }
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid, "elapsed_s": elapsed,
        "final": {k: final.get(k) for k in ("verdict", "stop_reason", "passed", "failed",
                                            "coverage")},
        "checks": checks, "gen_php_files": gen_files, "scripts": scripts,
        "rounds": rounds, "cases_sample": cases[:12],
        "llm_calls": len(llm), "total_tokens": tokens, "tmp_db": str(TMP_DB),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"smoke_php_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-php] 终态 {final.get('verdict')} | 轮次 "
          f"{[(r['round_no'], r['passed'], r['failed']) for r in rounds]}"
          f" | llm {len(llm)} 次 {tokens} tokens | {elapsed}s")
    print(f"[smoke-php] 证据 → {out}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
