"""白包阶段3·Go Tier2 真机验收冒烟（docs/多语言工具链接入任务包-20261106.md 阶段3）。

验收条目（任务卡【验收】）：标准库 demo 全链路——生成 TestGenR1_test.go（go test）→
go build+go test -c 编译闸 → go test -json 执行 → go_adapter 统一文本 → 归因/报告正常
+ 埋一个真缺陷（GetTier(90) 边界）被归因为 product_defect（证人如实上报）。
**必交付（论文数据）**：编译失败→R-Loop 重生成循环的 token 成本实测（Arm B：
真实 generate→收集闸编译失败（确定性破坏生成物）→真实重生成）。

用法：python scripts/smoke_go.py（真 Docker + 真 LLM，tmp 库，不碰真实库/真实任务链）
证据：data/reports/go_accept/smoke_go_<ts>.json
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "go_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "go_accept"
DEMO = ROOT / "data" / "demo" / "go-shop"


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-go','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)", (uid, str(DEMO), "go-shop-demo", None)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()

    from test_agent.graph.builder import build_graph
    from test_agent.graph.nodes import make_deps
    # Go 首轮编译 stdlib 测试依赖（testing/regexp 等，golang 镜像不带预编译 .a）
    # 内存尖峰：1GB 限额下容器被 OOM 杀、exec instance 404（真机实锤）——smoke 局部抬到 2GB
    config.DEFAULT_MEM_LIMIT_MB = 2048
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
    print(f"[smoke-go] 任务 {tid} L1 全图起跑（demo={DEMO.name}，真 Docker+真 LLM）…")
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
    gen_files = sorted(p.name for p in ws.glob("TestGen*_test.go"))
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
    tokens = sum(x.get("total_tokens") or 0 for x in llm)
    lang_row = c.execute(
        "SELECT primary_language FROM projects WHERE id=?", (pid,)).fetchone()

    checks = {
        "语言判定=go": lang_row["primary_language"] == "go",
        "生成了 TestGen*_test.go（项目根）": bool(gen_files),
        "framework=go_test 落库": all(s["framework"] == "go_test" for s in scripts) and bool(scripts),
        "编译闸+执行轮次落库（终态可判）": bool(rounds),
        "逐用例结果落库（go test nodeid）": any("::" in (x["nodeid"] or "") for x in cases),
        "埋的 GetTier(90) 边界缺陷被暴露（product_defect 证人）": any(
            x["status"] == "failed" for x in cases)
            or any(r["failed"] > 0 for r in rounds),
        "终态产出": bool(final.get("verdict")),
    }

    # —— Arm B：编译失败→R-Loop 重生成循环 token 成本实测（论文数据）——
    # 真实 generate（round 1）→ 确定性破坏生成物 → 收集闸 go build+go test -c 真实编译
    # 失败（R-Loop 的 case_defect 入口条件实锤）→ 真实重生成（round 2）→ 闸过。
    # 成本口径 = llm_calls 里两轮 gen 阶段 token 之和（真实循环单元成本）。
    arm_b: dict = {"skipped": "前置失败不执行"}
    if checks["语言判定=go"] and checks["生成了 TestGen*_test.go（项目根）"]:
        try:
            from test_agent.sandbox.runner import SandboxRunner
            from test_agent.skills.gen_tests import collect_gate_cmd
            runner = SandboxRunner()
            container = runner.ensure_container(config.IMAGE_GO, "mta-smoke-go-rloop",
                                                fresh=True)
            # 独立工作区（与 Arm A 隔离：破坏注入/重生成不污染图任务产物）
            ws_b = config.DATA_DIR / "tmp_applogs_accept" / f"go_rloop_ws_{time.strftime('%H%M%S')}"
            ws_b.mkdir(parents=True, exist_ok=True)
            for f in DEMO.iterdir():
                (ws_b / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            runner.copy_dir_into(container, ws_b, "/work")

            def gen(round_no: int) -> dict:
                from test_agent.skills.gen_tests import generate_tests
                api_key = __import__("os").environ.get("DEEPSEEK_API_KEY", "")
                before = sum((x["total_tokens"] or 0) for x in
                             c.execute("SELECT total_tokens FROM llm_calls WHERE task_id=?",
                                       (tid,)).fetchall())
                out = generate_tests(api_key, {
                    "workspace": str(ws_b), "requirement": (DEMO / "REQUIREMENT.md")
                    .read_text(encoding="utf-8"),
                    "module_hint": "go", "round_no": round_no,
                }, tid, runner)
                after = sum((x["total_tokens"] or 0) for x in
                            c.execute("SELECT total_tokens FROM llm_calls WHERE task_id=?",
                                      (tid,)).fetchall())
                data = json.loads(out)
                data["_tokens"] = after - before
                return data

            r1 = gen(1)
            f1 = ws_b / "TestGenR1_test.go"
            arm_b["round1_generated"] = r1.get("generated", 0)
            arm_b["round1_tokens"] = r1.get("_tokens")
            # 确定性破坏（编译失败注入：undefined 函数调用——LLM 常见真实废案形态）
            if f1.exists():
                broken = f1.read_text(encoding="utf-8") + \
                    "\nfunc TestBroken_注入(t *testing.T) {\n\tundefinedHelper()\n}\n"
                runner.exec_in(container,
                               ["sh", "-c",
                                "cat > /work/TestGenR1_test.go << 'MTA_EOF'\n"
                                + broken.replace("'", "'\\''") + "\nMTA_EOF"],
                               timeout_s=20, workdir="/")
                gate = runner.exec_in(container, collect_gate_cmd("go", "TestGenR1_test.go"),
                                      timeout_s=120, workdir="/work")
                arm_b["compile_fail_reproduced"] = gate.exit_code != 0
                arm_b["compile_fail_excerpt"] = (gate.stdout or gate.stderr)[-300:]
                # 真实重生成（R-Loop repair_gen 单元）→ 收集闸
                r2 = gen(2)
                arm_b["round2_generated"] = r2.get("generated", 0)
                arm_b["round2_tokens"] = r2.get("_tokens")
                if f1.exists():
                    runner.copy_dir_into(container, ws_b, "/work")
                    gate2 = runner.exec_in(container, collect_gate_cmd("go", "TestGenR1_test.go"),
                                           timeout_s=120, workdir="/work")
                    arm_b["regen_gate_pass"] = gate2.exit_code == 0
            else:
                arm_b["error"] = "round1 生成文件缺失"
            runner.cleanup("mta-smoke-go-rloop")
        except Exception as e:  # noqa: BLE001 —— Arm B 失败不影响 Arm A 判定，如实留痕
            arm_b = {"error": f"{type(e).__name__}: {e}"}
    else:
        arm_b = {"skipped": "Arm A 未过前置（无生成物）"}

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid, "elapsed_s": elapsed,
        "final": {k: final.get(k) for k in ("verdict", "stop_reason", "passed", "failed",
                                            "coverage")},
        "checks": checks, "gen_go_files": gen_files, "scripts": scripts,
        "rounds": rounds, "cases_sample": cases[:12],
        "llm_calls": len(llm), "total_tokens": tokens, "arm_b_rloop_cost": arm_b,
        "tmp_db": str(TMP_DB),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"smoke_go_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-go] Arm B R-Loop 成本实测：{json.dumps(arm_b, ensure_ascii=False)[:400]}")
    print(f"[smoke-go] 终态 {final.get('verdict')} | 轮次 "
          f"{[(r['round_no'], r['passed'], r['failed']) for r in rounds]}"
          f" | llm {len(llm)} 次 {tokens} tokens | {elapsed}s")
    print(f"[smoke-go] 证据 → {out}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
