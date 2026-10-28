"""StateGraph 组装 + run_task 入口。

    主图（对应论文"五阶段 + 带熔断的补测循环"插图）：

        clone → analyze → plan(RTM) → provision → generate → run → evaluate ─┬─ pass/stop → report → review ─┬─ 审查过 → END
                  ↑               ↑__________________________________________├─ repair_env（装依赖重跑）        └─ 审查 reject → generate（回炉吃预算）
                  └───────────────(repair_gen：剪坏用例→缺口回灌→重生成)──────┘

analyze 遇到非深度支持语言时短路到 report（诚实降级）。
report 后必经 review（评估五工具 + 报告二次审查）；review 只推翻 pass 判定回炉，
stop（product_defect 证人冻结）不被审查改判。
"""
import logging

from langgraph.graph import END, StateGraph

from .. import config
from ..store.db import get_conn
from .nodes import GraphDeps, make_deps, make_nodes
from .state import TaskState

log = logging.getLogger(__name__)


def build_graph(deps: GraphDeps):
    nodes = make_nodes(deps)
    g = StateGraph(TaskState)
    for name in ("clone", "analyze", "plan", "provision", "generate", "run",
                 "evaluate", "report", "review"):
        g.add_node(name, nodes[name])
    g.set_entry_point("clone")
    g.add_edge("clone", "analyze")
    g.add_conditional_edges("analyze",
                            lambda s: "report" if s.get("done") else "plan",
                            {"report": "report", "plan": "plan"})
    g.add_edge("plan", "provision")
    # provision 可能诚实短路（镜像未就绪且未开自动准备，沙箱自适应模式①）：直接进 report
    g.add_conditional_edges("provision",
                            lambda s: "report" if s.get("done") else "generate",
                            {"report": "report", "generate": "generate"})
    g.add_edge("generate", "run")
    # run 可能诚实短路（如 Java 链路占位，A7）：done=True 直接进 report，不进 evaluate
    g.add_conditional_edges("run",
                            lambda s: "report" if s.get("done") else "evaluate",
                            {"report": "report", "evaluate": "evaluate"})
    g.add_conditional_edges("evaluate",
                            lambda s: "report" if s["verdict"] in ("pass", "stop")
                            else ("provision" if s["verdict"] == "repair_env" else "generate"),
                            {"report": "report", "provision": "provision", "generate": "generate"})
    g.add_edge("report", "review")
    g.add_conditional_edges("review",
                            lambda s: "generate" if s.get("verdict") == "repair_gen" else END,
                            # 映射 key 必须是路由返回值的字面量：END 常量内部值是 "__end__"，
                            # 写成 "end" 会在运行时 KeyError '__end__'（实测踩坑，勿改回）
                            {"generate": "generate", "__end__": END})
    return g.compile()


def run_task(task_id: int) -> dict:
    """端到端入口：确认门 → 组图 → 执行 → 返回终态。容器无论成败一律回收。"""
    deps = make_deps()
    conn = deps.conn
    task = conn.execute(
        """SELECT t.requirement_text, t.confirmed, t.auto_prepare, p.repo_url
           FROM test_tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.id = ?""", (task_id,)).fetchone()
    if not task:
        raise ValueError(f"任务 {task_id} 不存在")
    if task["confirmed"] != 1:
        raise RuntimeError("任务未确认（confirmed=0），禁止下发执行 —— fail-closed 确认门")

    # A4：抢占式置位乐观锁——任务已在执行链路中时拒绝并发下发
    lock = conn.execute(
        """UPDATE test_tasks SET status='cloning', updated_at=datetime('now')
           WHERE id=? AND status NOT IN ('cloning','analyzing','provisioning',
                                         'generating','running','reporting')""",
        (task_id,))
    conn.commit()
    if lock.rowcount == 0:
        raise RuntimeError("任务正在执行中，禁止并发下发")

    # A3：预清理上次运行残留的容器，保证本轮从干净容器起步（R-Loop 轮次内仍复用）
    try:
        deps.runner.cleanup(f"mta-run-{task_id}")
    except Exception as e:  # 预清理失败不阻断，provision 的 ensure_container 会兜底
        log.warning("预清理容器失败: %s", e)

    cur = conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
                       (task_id,))
    conn.commit()
    init: TaskState = {
        "task_id": task_id,
        "session_id": cur.lastrowid,
        "repo_url": task["repo_url"],
        "requirement": task["requirement_text"] or "",
        "container_name": f"mta-run-{task_id}",
        "round_no": 0,
        "coverage_target": config.COVERAGE_TARGET,
        "fingerprints": [],
        "auto_prepare": bool(task["auto_prepare"]) if "auto_prepare" in task.keys() else False,
    }
    graph = build_graph(deps)
    try:
        final = graph.invoke(init, {"recursion_limit": 60})
        return final
    except Exception as e:
        conn.execute("UPDATE test_tasks SET status='failed', error=?, updated_at=datetime('now') WHERE id=?",
                     (f"{type(e).__name__}: {e}"[:500], task_id))
        conn.commit()
        raise
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{task_id}")
        except Exception as e:  # 回收失败不影响主流程，但要留痕
            log.warning("容器回收失败: %s", e)
