"""L2 集成层端到端冒烟（P1 出口判据：L2 端到端可演示）。

用法：python scripts/smoke_l2.py [demo_dir]
流程：建任务（代确认）→ 快照 demo 项目进工作区（模拟 L1 clone 产物）→ run_l2_task →
打印 leg 台账 + 用例分布 + 判定。检查 test_levels/interface_endpoints/call_edges/
evaluation_rounds(level='L2') 四处落库。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402
from test_agent.graph.l2 import run_l2_task  # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "demo" / "L2场景-coupon-api"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    demo = Path(args[0]) if args else DEMO
    init_db()
    conn = get_conn()
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(demo), f"smoke-l2-{demo.name}")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (demo / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()

    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(demo, ws, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"[smoke-l2] 任务 {tid}，工作区 {ws}")

    final = run_l2_task(tid)

    conn2 = get_conn()
    leg = conn2.execute("SELECT * FROM test_levels WHERE task_id=? AND level='L2'",
                        (tid,)).fetchone()
    er = conn2.execute("SELECT passed, failed, verdict, stop_reason FROM evaluation_rounds"
                       " WHERE task_id=? AND level='L2'", (tid,)).fetchone()
    cases = conn2.execute(
        """SELECT c.method, COUNT(*) n FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
           WHERE s.task_id=? GROUP BY c.method""", (tid,)).fetchall()
    eps = conn2.execute("SELECT COUNT(*) n FROM interface_endpoints WHERE task_id=?",
                        (tid,)).fetchone()["n"]
    edges = conn2.execute("SELECT COUNT(*) n FROM call_edges WHERE task_id=?",
                          (tid,)).fetchone()["n"]
    print(json.dumps({
        "task_id": tid, "leg_status": leg["status"], "leg_strategy": leg["strategy"],
        "verdict": final.get("verdict"), "stop_reason": final.get("stop_reason"),
        "rounds": dict(er) if er else None,
        "cases_by_method": {r["method"]: r["n"] for r in cases},
        "endpoints": eps, "call_edges": edges,
    }, ensure_ascii=False, indent=1))
    if final.get("last_stdout"):
        print("\n===== pytest 原始输出尾部 =====")
        print(final["last_stdout"][-2500:])
    if "--keep-ws" not in sys.argv:
        shutil.rmtree(ws, ignore_errors=True)   # 冒烟工作区不残留（grade 重放不需要）
    else:
        print(f"（工作区保留：{ws}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
