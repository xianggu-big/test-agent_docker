"""控制变量对照格驱动（docs/链路方法测试文档.md §5 X1-X4，协议见同文档 §1-§3）。

用法：python scripts/run_grid.py X1|X2|X3|X4
每格 = 默认配置上只改一个开关（§3 矩阵）→ 新建 corpus-001 任务 → run_task 端到端 →
打印摘要行。阅卷再跑：python scripts/grade_corpus.py <task_id> corpus-001 "<格标签>"。

纪律：每次只动一格；跑之前先在文档 §5/§6 登记；结果如实记录（含 stop）。
确认门说明：实验任务由本脚本代确认（confirmed=1）——跑格指令本身即用户下达的实验授权。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402
from test_agent.graph.builder import run_task  # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS = "corpus-001"
REPO = str(ROOT / "data" / "corpus" / CORPUS / "project")
REQUIREMENT = (ROOT / "data" / "corpus" / CORPUS / "project" / "REQUIREMENT.md").read_text(encoding="utf-8")

# §3 控制变量矩阵（每格只改一个开关，其余保持默认）
GRID = {
    "X1": {},                                   # 默认组（校准/复跑用）
    "X2": {"GEN_METHODS_ENABLED": False},       # V2=False：无五方法骨架，自由发挥
    "X3": {"PLAN_ENABLED": False},              # V3=False：跳过 plan/RTM 测试条件
    "X4": {"COVERAGE_FEEDBACK_ENABLED": False}, # V4=False：无覆盖缺口回灌（盲补）
}


def main() -> int:
    cell = sys.argv[1] if len(sys.argv) > 1 else ""
    if cell not in GRID:
        print(__doc__)
        print(f"格子必须是 {list(GRID)} 之一")
        return 1
    overrides = GRID[cell]
    init_db()
    conn = get_conn()
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, REPO, f"grid-{cell}-{CORPUS}")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by) "
        "VALUES(?,?,?,?,1)", (pid, uid, REQUIREMENT, uid)).lastrowid
    conn.commit()

    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    print(f"[grid {cell}] 任务 {tid} 启动，开关覆盖：{overrides or '（默认组）'}")
    try:
        final = run_task(tid)
    except Exception as e:  # noqa: BLE001 —— 实验格失败也要留痕继续
        print(f"[grid {cell}] 任务 {tid} 异常终止：{type(e).__name__}: {e}")
        return 1
    finally:
        for k, v in saved.items():
            setattr(config, k, v)

    summary = {"cell": cell, "task_id": tid, "verdict": final.get("verdict"),
               "stop_reason": final.get("stop_reason"), "rounds": final.get("round_no"),
               "passed": final.get("passed"), "failed": final.get("failed"),
               "coverage": final.get("coverage_line")}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"阅卷：python scripts/grade_corpus.py {tid} {CORPUS} \"{cell} 对照格\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
