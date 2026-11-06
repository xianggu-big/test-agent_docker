"""白包阶段1·TypeScript Tier2 真机验收冒烟（docs/多语言工具链接入任务包-20261106.md 阶段1）。

用法：python scripts/smoke_ts.py
验收条目（任务卡【验收】逐条）：TS demo（tsconfig + ts-jest）真机全链路——
生成 .test.ts → tsc 类型闸 → 容器 jest 执行 → 覆盖率映射回 .ts → 归因/报告正常。

实现说明：直接镜像 run_task 的图驱动体（build_graph + graph.invoke，同 init 形态），
不经 run_task/run_task_levels 入口（闲时任务不跑真实任务链的护栏口径），全程 tmp 库。
护栏：不写真实库 data/test_agent.db；demo 本地路径做 repo_url；容器 finally 回收。
产物：data/reports/applogs_accept/../ts_accept/smoke_ts_<ts>.json（证据留档）。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.sandbox.runner import SandboxRunner  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "ts_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "ts_accept"
DEMO = ROOT / "data" / "demo" / "ts-calc"


def verify_sourcemap() -> tuple[bool, str]:
    """零 LLM 容器臂（任务卡验收「覆盖率映射回 .ts」的直接证据）：
    demo 源 + 2 条确定性用例 → install_deps 同款补装 → build_run_cmd_js 真实命令 →
    断言 jest coverageMap 键为 .ts（istanbul sourceMap 回映射）+ adapter 文本含 .ts 覆盖行。
    不走 test_reports.per_file——那是 B2 缺口回灌口径（全覆盖文件行有意不进，test_prune 守卫）。"""
    import json
    from test_agent.graph.nodes import build_run_cmd_js, _pkg_declares_jest, _inline_jest_config
    from test_agent.skills.js_adapter import jest_json_to_pytest_text
    runner = SandboxRunner()
    name = "mta-smoke-ts-map"
    try:
        c = runner.ensure_container("mta-test:node20-jest", name)
        runner.exec_in(c, ["sh", "-c", "mkdir -p /work/src"])
        calc = (DEMO / "src" / "calc.ts").read_text(encoding="utf-8")
        runner.exec_in(c, ["sh", "-c", f"cat > /work/src/calc.ts << 'TSEOF'\n{calc}TSEOF"])
        runner.exec_in(c, ["sh", "-c", 'cat > /work/test_gen_r1.test.ts << \'TSEOF\'\n'
                           'import { add, clamp } from "./src/calc";\n'
                           'test("test_add_pos", () => { expect(add(1, 2)).toBe(3); });\n'
                           'test("test_clamp_hi", () => { expect(clamp(11, 0, 10)).toBe(10); });\nTSEOF'])
        runner.exec_in(c, ["npm", "install", "--no-save", "--ignore-scripts", "--no-audit",
                           "--no-fund", "ts-jest@29", "typescript@5", "@types/jest@29",
                           "@types/node@20"], timeout_s=300)
        cmd = build_run_cmd_js(round_no=1)
        if not _pkg_declares_jest(DEMO):
            cmd += ["--config", _inline_jest_config(include_ts=True)]
        r = runner.exec_in(c, cmd, timeout_s=180)
        payload = json.loads(r.stdout)
        cov_keys = list(payload.get("coverageMap", {}).keys())
        text = jest_json_to_pytest_text(r.stdout, r.stderr)
        ok = any(str(k).endswith(".ts") for k in cov_keys) and "src/calc.ts" in text
        detail = (f"coverageMap keys={cov_keys} | adapter 文本含 src/calc.ts 行="
                  f"{'src/calc.ts' in text} | numPassed={payload.get('numPassedTests')} "
                  f"numFailed={payload.get('numFailedTests')}")
        return ok, detail
    except Exception as e:  # noqa: BLE001 —— 容器臂失败如实呈现，不炸整体
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            runner.cleanup(name)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB                    # 任何 get_conn 之前
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-ts','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)",
                       (uid, str(DEMO), "ts-calc-demo", None)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()

    # —— 镜像 run_task 体（builder.py:144）：锁位→预清容器→会话→init→图驱动 ——
    from test_agent.graph.builder import build_graph
    from test_agent.graph.nodes import make_deps
    deps = make_deps()
    conn2 = deps.conn
    lock = conn2.execute(
        """UPDATE test_tasks SET status='cloning', updated_at=datetime('now')
           WHERE id=? AND status NOT IN ('cloning','analyzing','provisioning',
                                         'generating','running','reporting')""", (tid,))
    conn2.commit()
    assert lock.rowcount == 1, "任务被锁（并发态）——冒烟前置失败"
    deps.runner.cleanup(f"mta-run-{tid}")
    cur = conn2.execute(
        "INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')", (tid,))
    conn2.commit()
    init = {"task_id": tid, "session_id": cur.lastrowid, "repo_url": str(DEMO),
            "requirement": (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"),
            "container_name": f"mta-run-{tid}", "round_no": 0,
            "coverage_target": config.COVERAGE_TARGET, "fingerprints": [], "auto_prepare": False}
    graph = build_graph(deps)
    print(f"[smoke-ts] 任务 {tid} L1 全图起跑（demo={DEMO.name}，真 Docker+真 LLM）…")
    t0 = time.time()
    try:
        final = graph.invoke(init, {"recursion_limit": 60})
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{tid}")
        except Exception as e:   # noqa: BLE001
            print("容器回收失败:", e)
    elapsed = round(time.time() - t0, 1)

    # —— 证据收集（确定性 SQL/文件读取，零 LLM） ——
    c = get_conn()
    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    ts_files = sorted(p.name for p in ws.glob("test_gen_*.test.ts")) if ws.exists() else []
    scripts = [dict(r) for r in c.execute(
        "SELECT framework, filename, version FROM test_scripts WHERE task_id=?", (tid,))]
    rounds = [dict(r) for r in c.execute(
        "SELECT round_no, passed, failed, verdict FROM evaluation_rounds "
        "WHERE task_id=? ORDER BY round_no", (tid,))]
    cases = [dict(r) for r in c.execute(
        "SELECT round_no, nodeid, status, origin FROM test_case_results "
        "WHERE task_id=? ORDER BY id DESC LIMIT 20", (tid,))]
    rep = c.execute("SELECT per_file, defects FROM test_reports WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    per_file = rep["per_file"] if rep else None
    try:
        per_file_parsed = json.loads(per_file) if per_file else {}
    except (TypeError, ValueError):
        per_file_parsed = {}
    try:
        defects = json.loads(rep["defects"]) if rep and rep["defects"] else {}
    except (TypeError, ValueError):
        defects = {}
    llm = [dict(r) for r in c.execute(
        "SELECT phase, model, total_tokens FROM llm_calls WHERE task_id=?", (tid,))]
    tokens = sum(x.get("total_tokens") or 0 for x in llm)
    lang_row = c.execute(
        "SELECT primary_language FROM projects WHERE id=?", (pid,)).fetchone()

    sm_ok, sm_detail = verify_sourcemap()
    print(f"[smoke-ts] 覆盖率映射容器臂: {'PASS' if sm_ok else 'FAIL'} | {sm_detail}")

    checks = {
        "语言判定=typescript": lang_row["primary_language"] == "typescript",
        "生成了 .test.ts": bool(ts_files),
        "framework=jest 落库": all(s["framework"] == "jest" for s in scripts) and bool(scripts),
        "执行轮次落库（R-Loop 参与且终态可判）": bool(rounds),
        # jest 逐用例 nodeid=fullName（用例名形态，不带文件前缀——case_results.parse_jest_results 口径）
        "逐用例结果落库（jest fullName 形态）": any(
            (x["nodeid"] or "").startswith("test_") for x in cases),
        "覆盖率映射回 .ts（sourceMap，容器臂直接验 coverageMap 键）": sm_ok,
        "埋的 clamp 缺陷被暴露（失败用例在案）": any(
            x["status"] == "failed" for x in cases) or defects.get("failed", 0) > 0
        or any(r["failed"] > 0 for r in rounds),
        "终态产出": bool(final.get("verdict")),
    }
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid, "elapsed_s": elapsed,
        "final": {k: final.get(k) for k in ("verdict", "stop_reason", "passed", "failed",
                                            "coverage")},
        "checks": checks,
        "gen_ts_files": ts_files, "scripts": scripts, "rounds": rounds,
        "cases_sample": cases[:12], "per_file_keys": list(per_file_parsed)[:8],
        "defects": {k: defects.get(k) for k in ("failure_kind", "stop_reason")},
        "llm_calls": len(llm), "total_tokens": tokens, "tmp_db": str(TMP_DB),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"smoke_ts_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-ts] 终态 {final.get('verdict')} | 轮次 {[(r['round_no'], r['passed'], r['failed']) for r in rounds]}"
          f" | llm {len(llm)} 次 {tokens} tokens | {elapsed}s")
    print(f"[smoke-ts] 证据 → {out_path}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
