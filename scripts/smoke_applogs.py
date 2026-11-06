"""黑盒能力补齐阶段1·真机验收冒烟（docs/黑盒能力补齐任务包-20261106.md 阶段1【验收】）。

用法：python scripts/smoke_applogs.py
两臂验收（与任务卡验收条目逐条对应，缺一即 FAIL）：
  A 埋错应用臂：demo /boom 抛 RuntimeError → 完整 L2 leg → app_logs 按轮分档有行 +
     Traceback/ERROR 被正则命中 + 宿主快照落盘 + log_analyze（真 LLM）叙述引用真实行号；
  B 端口占用臂：demo 导入时占住 8000 → 服务启动失败路径也分流落库（环境错误证据）。
护栏：全程 tmp 库（不写真实库 data/test_agent.db）；工作区落在 data/workspaces/task_<tid>
（真实流程行为，不动既有目录）；容器由 run_l2_task finally 回收。
产物：data/reports/applogs_accept/accept_<ts>.json（证据留档）。
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "accept.db"
REPORT_DIR = config.DATA_DIR / "reports" / "applogs_accept"


def _mk_task(demo: Path, conn) -> int:
    row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    uid = row["id"] if row else conn.execute(
        "INSERT INTO users(username,password_hash) VALUES('smoke-applogs','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(demo), f"applogs-{demo.name}")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (demo / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()
    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    if ws.exists():
        shutil.rmtree(ws)                     # 只清本冒烟自建的 tmp 库任务工作区
    shutil.copytree(demo, ws, ignore=shutil.ignore_patterns("__pycache__"))
    return tid


def main() -> int:
    # —— 全程 tmp 库：改 config.DB_PATH 必须发生在任何 get_conn 之前 ——
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    from test_agent.graph.l2 import run_l2_task
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tmp_db": str(TMP_DB), "arms": {}}

    # —— A 埋错应用臂：完整 L2 leg（extract→callgraph→provision→generate→run→evaluate） ——
    demo_a = ROOT / "data" / "demo" / "L2场景-applogs"
    tid_a = _mk_task(demo_a, get_conn())
    print(f"[A] 任务 {tid_a} L2 leg 起跑（demo={demo_a.name}，/boom 埋错）…")
    final_a = run_l2_task(tid_a)
    conn = get_conn()
    rows_a = [dict(r) for r in conn.execute(
        "SELECT round_no, level, kind, line_no, message FROM app_logs"
        " WHERE task_id=? ORDER BY line_no", (tid_a,))]
    snap_a = config.DATA_DIR / "workspaces" / f"task_{tid_a}" / "logs" / "app_r1.log"
    arm_a = {
        "final_verdict": final_a.get("verdict"),
        "stop_reason": (final_a.get("stop_reason") or "")[:200],
        "app_logs_rows": len(rows_a),
        "kinds": sorted({r["kind"] for r in rows_a}),
        "traceback_hit": any(r["kind"] == "python_traceback" for r in rows_a),
        "runtime_error_hit": any("planted app error" in (r["message"] or "") for r in rows_a),
        "snapshot": {"exists": snap_a.exists(),
                     "lines": len(snap_a.read_text(encoding="utf-8", errors="replace").splitlines())
                     if snap_a.exists() else 0,
                     "path": str(snap_a)},
        "rows_sample": rows_a[:12],
    }
    if rows_a:                                 # log_analyze：真 LLM，引用闸在模块内
        import os
        from test_agent.skills import log_analyze as la
        out = la.run_log_analyze(os.environ["DEEPSEEK_API_KEY"], conn, tid_a)
        arm_a["log_analyze"] = {k: out.get(k) for k in
                                ("citations_ok", "summary", "error", "round_no")}
        arm_a["log_analyze_items"] = out.get("items")
    report["arms"]["A_planted_defect"] = arm_a
    conn.close()

    # —— B 端口占用臂：服务起不来的日志也必须分档入库（环境错误证据） ——
    demo_b = ROOT / "data" / "demo" / "L2场景-applogs-portbind"
    tid_b = _mk_task(demo_b, get_conn())
    print(f"[B] 任务 {tid_b} L2 leg 起跑（demo={demo_b.name}，预期 provision 端口占用失败）…")
    final_b = run_l2_task(tid_b)
    conn = get_conn()
    rows_b = [dict(r) for r in conn.execute(
        "SELECT round_no, kind, line_no, message FROM app_logs"
        " WHERE task_id=? ORDER BY line_no", (tid_b,))]
    arm_b = {
        "final_verdict": final_b.get("verdict"),
        "stop_reason": (final_b.get("stop_reason") or "")[:200],
        "app_logs_rows": len(rows_b),
        "port_conflict_hit": any(
            ("in use" in (r["message"] or "")) or ("Errno 98" in (r["message"] or ""))
            or ("EADDRINUSE" in (r["message"] or "")) for r in rows_b),
        "rows_sample": rows_b[:12],
    }
    report["arms"]["B_port_conflict"] = arm_b
    conn.close()

    # —— 验收判定（任务卡验收条目逐条映射） ——
    checks = {
        "A_app_logs按轮分档有行": arm_a["app_logs_rows"] > 0,
        "A_埋的运行时错误被正则命中": arm_a["runtime_error_hit"],
        "A_宿主快照落盘": arm_a["snapshot"]["exists"],
        "A_log_analyze引用真实行号": bool(arm_a.get("log_analyze", {}).get("citations_ok")),
        "B_端口占用被正则命中": arm_b["port_conflict_hit"],
    }
    report["checks"] = checks
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"accept_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-applogs] 证据 → {out_path}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
