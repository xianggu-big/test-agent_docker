"""StateGraph 组装 + run_task 入口。

主图（对应论文"五阶段 + 带熔断的补测循环"插图）：

    clone → analyze → provision → generate → run → evaluate ─┬─ pass/stop → report → END
                  ↑          ↑_______________________________├─ repair_env（装依赖重跑）
                  └──────────(repair_gen：重新生成用例)────────┘

analyze 遇到非深度支持语言时短路到 report（诚实降级）。
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
    for name in ("clone", "analyze", "provision", "generate", "run", "evaluate", "report"):
        g.add_node(name, nodes[name])
    g.set_entry_point("clone")
    g.add_edge("clone", "analyze")
    g.add_conditional_edges("analyze",
                            lambda s: "report" if s.get("done") else "provision",
                            {"report": "report", "provision": "provision"})
    g.add_edge("provision", "generate")
    g.add_edge("generate", "run")
    # run 可能诚实短路（如 Java 链路占位，A7）：done=True 直接进 report，不进 evaluate
    g.add_conditional_edges("run",
                            lambda s: "report" if s.get("done") else "evaluate",
                            {"report": "report", "evaluate": "evaluate"})
    g.add_conditional_edges("evaluate",
                            lambda s: "report" if s["verdict"] in ("pass", "stop")
                            else ("provision" if s["verdict"] == "repair_env" else "generate"),
                            {"report": "report", "provision": "provision", "generate": "generate"})
    g.add_edge("report", END)
    return g.compile()


def run_task(task_id: int) -> dict:
    """端到端入口：确认门 → 组图 → 执行 → 返回终态。容器无论成败一律回收。"""
    deps = make_deps()
    conn = deps.conn
    task = conn.execute(
        """SELECT t.requirement_text, t.confirmed, p.repo_url
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
