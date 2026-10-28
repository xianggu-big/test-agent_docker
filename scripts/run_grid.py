"""控制变量对照格驱动（docs/链路方法测试文档.md §5，协议见同文档 §1-§3）。

用法：python scripts/run_grid.py X1|X2|X3|X4|X5 [corpus_id]
每格 = 默认配置上只改一个开关（§3 矩阵）→ 新建语料任务 → run_task 端到端 →
打印摘要行。阅卷再跑：python scripts/grade_corpus.py <task_id> <corpus_id> "<格标签>"。

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

# §3 控制变量矩阵（每格只改一个开关，其余保持默认）
GRID = {
    "X1": {},                                   # 默认组（校准/复跑用）
    "X2": {"GEN_METHODS_ENABLED": False},       # V2=False：无五方法骨架，自由发挥
    "X3": {"PLAN_ENABLED": False},              # V3=False：跳过 plan/RTM 测试条件
    "X4": {"COVERAGE_FEEDBACK_ENABLED": False}, # V4=False：无覆盖缺口回灌（盲补）
    "X5": {"GEN_BOUNDARY_ATTACK": True},        # V6=True：边界攻击纪律（corpus-002 改进实验）
    # 三方案改造格（docs/设计方案对比实验.md）：同基线同语料，各改一层
    "A1": {"ASSERTION_GATE_ENABLED": True},     # 方案A：断言质量闸（独有指标=弱断言率）
    "B1": {"PBT_ENABLED": True},                # 方案B：属性测试第七法（独有指标=变异杀死率）
    "C1": {"COND_FEEDBACK_ENABLED": True},      # 方案C：条件缺口回灌（独有指标=RTM条件覆盖）
}


def main() -> int:
    args = sys.argv[1:]
    cell = args[0] if args else ""
    corpus = args[1] if len(args) > 1 else "corpus-001"
    if cell not in GRID:
        print(__doc__)
        print(f"格子必须是 {list(GRID)} 之一")
        return 1
    repo_dir = ROOT / "data" / "corpus" / corpus / "project"
    requirement = (repo_dir / "REQUIREMENT.md").read_text(encoding="utf-8")
    overrides = GRID[cell]
    init_db()
    conn = get_conn()
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(repo_dir), f"grid-{cell}-{corpus}")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by) "
        "VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid
    conn.commit()

    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    print(f"[grid {cell}/{corpus}] 任务 {tid} 启动，开关覆盖：{overrides or '（默认组）'}")
    try:
        final = run_task(tid)
    except Exception as e:  # noqa: BLE001 —— 实验格失败也要留痕继续
        print(f"[grid {cell}] 任务 {tid} 异常终止：{type(e).__name__}: {e}")
        return 1
    finally:
        for k, v in saved.items():
            setattr(config, k, v)

    summary = {"cell": cell, "corpus": corpus, "task_id": tid, "verdict": final.get("verdict"),
               "stop_reason": final.get("stop_reason"), "rounds": final.get("round_no"),
               "passed": final.get("passed"), "failed": final.get("failed"),
               "coverage": final.get("coverage_line")}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"阅卷：python scripts/grade_corpus.py {tid} {corpus} \"{cell} 对照格\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
