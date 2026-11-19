"""StateGraph 组装 + run_task / run_task_prep / run_task_exec / run_task_auto 入口。

    主图（对应论文"五阶段 + 带熔断的补测循环"插图）：

        clone → analyze → plan(RTM) → provision → generate → run → evaluate ─┬─ pass/stop → report → review ─┬─ 审查过 → END
                  ↑               ↑__________________________________________├─ repair_env（装依赖重跑）        └─ 审查 reject → generate（回炉吃预算）
                  └───────────────(repair_gen：剪坏用例→缺口回灌→重生成)──────┘

analyze 遇到非深度支持语言时短路到 report（诚实降级）。
report 后必经 review（评估五工具 + 报告二次审查）；review 只推翻 pass 判定回炉，
stop（product_defect 证人冻结）不被审查改判。

两阶段人机协作（docs/两阶段人机协作流程设计-20261031.md；任务级 two_stage=1 时启用）：
    准备图  clone → analyze → plan → generate ─┬─ 正常 → 停在 ready_for_review（待审查停等点）
                                                └─ analyze 诚实降级 → 任务如实 failed
    执行图  provision(布考场：按审查结论重写用例文件+补跑顺延收集闸) → run → evaluate ─┬─ 同主图
    确认=授权启动准备图；下发=启动执行图。R-Loop 归执行图（无人值守能力保住）。
"""
import json
import logging
import os

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
                 "evaluate", "report", "review", "static_draft"):
        g.add_node(name, nodes[name])
    g.set_entry_point("clone")
    g.add_edge("clone", "analyze")
    g.add_conditional_edges("analyze",
                            lambda s: "report" if s.get("done") else "plan",
                            {"report": "report", "plan": "plan"})
    # 静态模式（Tier3 分级，2026-11-01）：无容器执行链路的项目 plan 后进扫描+设计草稿，直达报告
    g.add_conditional_edges("plan",
                            lambda s: "static_draft" if s.get("static_only") else "provision",
                            {"static_draft": "static_draft", "provision": "provision"})
    g.add_edge("static_draft", "report")
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


def prep_gen_route(n_cases: int, round_no: int, max_rounds: int) -> str:
    """准备图出题路由（纯函数便于单测）：0 用例（LLM 偶发不可解析）给一次重试——
    单阶段主图有 repair_gen 循环兜底，两阶段准备图 generate 即 END，无重试则
    LLM 一次抖动就让用户看到空用例集（E2E 实锤 task_264）。仍 0 也停在待审查：
    评审页对话框增补（feedback 端点）就是用户侧自愈通道。"""
    if n_cases == 0 and round_no < max_rounds:
        return "generate"
    return "__end__"


def build_prep_graph(deps: GraphDeps):
    """准备图（两阶段·阶段一）：克隆→摸底→考纲→出题，跑完停在待审查。

    generate 的容器回灌+收集闸在无容器时诚实顺延（gen_tests 内置容忍），不弱化闸语义；
    出题 0 条自动重试一次（prep_gen_route），单阶段 repair_gen 兜底的两阶段对等物。"""
    nodes = make_nodes(deps)
    conn = deps.conn
    g = StateGraph(TaskState)
    for name in ("clone", "analyze", "plan", "generate", "static_draft"):
        g.add_node(name, nodes[name])
    g.set_entry_point("clone")
    g.add_edge("clone", "analyze")
    # analyze 诚实降级（非深度支持语言）：准备阶段无报告可出，END 后由 run_task_prep 如实失败
    g.add_conditional_edges("analyze",
                            lambda s: END if s.get("done") else "plan",
                            {"__end__": END, "plan": "plan"})
    # 静态模式项目：plan 后进扫描+草稿（生成的是设计草稿非可执行用例），停在待审查
    g.add_conditional_edges("plan",
                            lambda s: "static_draft" if s.get("static_only") else "generate",
                            {"static_draft": "static_draft", "generate": "generate"})
    g.add_edge("static_draft", END)

    def _gen_ok(s: TaskState) -> str:
        n = conn.execute(
            """SELECT COUNT(*) AS n FROM test_cases c JOIN test_scripts sc ON sc.id=c.script_id
               WHERE sc.task_id=? AND c.source='ai' AND c.enabled=1""",
            (s["task_id"],)).fetchone()["n"]
        return prep_gen_route(n, s.get("round_no", 0), config.PREP_GEN_MAX_ROUNDS)

    # 映射 key 必须是路由返回值字面量（run46 教训：END 内部值 "__end__"）
    g.add_conditional_edges("generate", _gen_ok,
                            {"generate": "generate", "__end__": END})
    return g.compile()


def _after_evaluate(s: TaskState) -> str:
    """evaluate 条件边（A2 2026-11-20 抽出为模块函数便于单测）：
    pass/stop → report；verify 冻结任务其余 verdict 一律 → report（不补题不重跑——
    同卷比较前提，Qoder 体检论断 2：修复前 repair_gen/repair_env 会重新生成/剪除
    用例）；普通任务 repair_env → provision 回炉、其余 → generate（R-Loop 保留）。"""
    if s["verdict"] in ("pass", "stop"):
        return "report"
    if s.get("verify_mode") == 1:
        return "report"
    return "provision" if s["verdict"] == "repair_env" else "generate"


def _after_review(s: TaskState) -> str:
    """review 条件边：verify 冻结任务不回炉（repair_gen 也 END——报告已落库如实呈现）；
    普通任务 repair_gen → generate（R-Loop 保留）。"""
    if s.get("verify_mode") == 1:
        return "__end__"
    return "generate" if s.get("verdict") == "repair_gen" else "__end__"


def build_exec_graph(deps: GraphDeps):
    """执行图（两阶段·阶段二）：布考场→开考→判卷→报告→复审，吃阶段一审查通过的用例集。

    首轮不进 generate（用例集已在阶段一出题并经人工/AI 审查）；generate 仅由
    repair_env/repair_gen/审查回炉再入——R-Loop 完整保留在阶段二（无人值守能力保住）。
    A2（2026-11-20）：verify_mode=1 的修复验证任务全图禁写用例——evaluate 非 pass/stop
    直接 report、review 一律 END（所有用例写入口：generate 主生成/fbN 增补/剪除/
    重写/delete-first 全部不可达）。"""
    nodes = make_nodes(deps)
    g = StateGraph(TaskState)
    for name in ("provision", "generate", "run", "evaluate", "report", "review"):
        g.add_node(name, nodes[name])
    g.set_entry_point("provision")
    g.add_conditional_edges("provision",
                            lambda s: "report" if s.get("done") else "run",
                            {"report": "report", "run": "run"})
    g.add_conditional_edges("run",
                            lambda s: "report" if s.get("done") else "evaluate",
                            {"report": "report", "evaluate": "evaluate"})
    g.add_conditional_edges("evaluate", _after_evaluate,
                            {"report": "report", "provision": "provision", "generate": "generate"})
    g.add_edge("generate", "run")
    g.add_edge("report", "review")
    g.add_conditional_edges("review", _after_review,
                            {"generate": "generate", "__end__": END})
    return g.compile()


def run_task(task_id: int) -> dict:
    """端到端入口：确认门 → 组图 → 执行 → 返回终态。容器无论成败一律回收。
    守卫走临时短连接（A4 2026-11-20：拒绝路径零 Docker/注册表开销，与 prep/exec 同款）。"""
    guard = get_conn()
    task = guard.execute(
        """SELECT t.requirement_text, t.confirmed, t.status, t.auto_prepare, p.repo_url
           FROM test_tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.id = ?""", (task_id,)).fetchone()
    if not task:
        guard.close()
        raise ValueError(f"任务 {task_id} 不存在")
    if task["confirmed"] != 1:
        guard.close()
        raise RuntimeError("任务未确认（confirmed=0），禁止下发执行 —— fail-closed 确认门")
    if task["status"] == "cancelled":
        guard.close()
        raise RuntimeError("任务已取消，禁止执行（A4 取消竞态守卫）——如需重跑请重新下发")
    guard.close()

    deps = make_deps()
    conn = deps.conn

    # A4：抢占式置位乐观锁——任务已在执行链路中时拒绝并发下发
    # （A4 2026-11-20：排除表补 cancelled——修复前已取消任务可被锁放行=取消后复活）
    lock = conn.execute(
        """UPDATE test_tasks SET status='cloning', updated_at=datetime('now')
           WHERE id=? AND status NOT IN ('cloning','analyzing','provisioning',
                                         'generating','running','reporting','cancelled')""",
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
        # 会话收口（2026-11-15 闲时找bug）：异常上抛前关掉本轮会话，不留永久"进行中"
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=? AND ended_at IS NULL",
            (init.get("session_id"),))
        close_container_jobs(conn, task_id, "failed")   # 台账收口（倒推实锤 1001 条滞留）
        conn.commit()
        raise
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{task_id}")
        except Exception as e:  # 回收失败不影响主流程，但要留痕
            log.warning("容器回收失败: %s", e)
        # DB sidecar 回收（卡 6：任务级生命周期，prepare_database 拉起 mta-dbsidecar-<tid>）
        try:
            deps.runner.cleanup(f"mta-dbsidecar-{task_id}")
        except Exception as e:
            log.warning("sidecar 回收失败: %s", e)


# ==================== 两阶段人机协作（docs/两阶段人机协作流程设计-20261031.md） ====================

def _load_task_full(conn, task_id: int):
    row = conn.execute(
        """SELECT t.*, p.repo_url, p.primary_language FROM test_tasks t
           JOIN projects p ON p.id = t.project_id WHERE t.id=?""", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"任务 {task_id} 不存在")
    return row


def _new_session(conn, task_id: int) -> int:
    cur = conn.execute("INSERT INTO agent_sessions(task_id, agent_role) VALUES (?, 'test_agent')",
                       (task_id,))
    conn.commit()
    return cur.lastrowid


def _fail_task(conn, task_id: int, reason: str) -> None:
    conn.execute("UPDATE test_tasks SET status='failed', error=?, updated_at=datetime('now') WHERE id=?",
                 (reason[:500], task_id))
    close_container_jobs(conn, task_id, "failed")
    conn.commit()


def close_container_jobs(conn, task_id: int, status: str = "failed") -> int:
    """容器台账收口（2026-11-16 任务列表倒推实锤）：container_jobs 1001 条滞留
    running——report 节点只走 happy path，图异常炸掉（E9 402/超时/服务重启）时
    provision 落的台账行没人收口。所有把任务带离 ACTIVE 的路径都必须调本函数
    （builder except×2 / queue._fail_task_if_pending / 两个启动 sweep）。
    返回收口行数（幂等：只动 running 行）。"""
    n = conn.execute(
        "UPDATE container_jobs SET status=?, finished_at=datetime('now')"
        " WHERE task_id=? AND status='running'", (status, task_id)).rowcount
    return n


def _empty_set_message(reason: str | None) -> str:
    """出题 0 条的空集提示——按 gen_tests 实际返回的 reason 归因，禁止硬编码猜测
    （2026-11-09 任务卡-LLM模型选择 B 项：原文案硬编码"LLM 输出两次不可解析"，
    但 local 档实际原因是"无 Key 跳过"，误导排查方向）。四种典型原因各有如实文案；
    skill 自身失败（2026-11-10 起 gen_reason=生成工具失败：…）如实透传 error；
    未知原因兜底为"原因未知（详见 generate 轮消息）"，绝不编造。"""
    r = (reason or "").strip()
    if r.startswith("生成工具失败"):
        return (f"[prep] 出题 0 条——原因：{r}。"
                f"处理：本地模型请确认 ollama serve 在跑；云端档检查 Key 与余额")
    if "未配置 Key" in r or "skip_reason" in r:
        return f"[prep] 出题 0 条——原因：{r}。可在设置页检查 LLM 模型与 Key 配置后重试"
    if "无法解析" in r or "不可解析" in r:
        return ("[prep] 出题 0 条——原因：LLM 输出无法解析（原始输出已留档 llm_parse_fail/）。"
                "多为需求条件过多导致输出截断——可在左侧「对话增补」描述你要的用例，"
                "AI 会定向补生成")
    if "静态审查" in r:
        return ("[prep] 出题 0 条——原因：全部用例未过静态审查（逐条剔除留痕见 generate 轮消息）。"
                "请核对需求与被测源码是否对得上")
    if "模板" in r:
        return "[prep] 出题 0 条——原因：全部用例模板展开失败（槽位无效），可重试或调整需求描述"
    return ("[prep] 出题 0 条——原因未知（详见 generate 轮消息）。"
            "可在左侧「对话增补」描述你要的用例，AI 会定向补生成")


def run_task_prep(task_id: int) -> dict:
    """两阶段·阶段一入口：确认门 → 准备图（克隆/摸底/考纲/出题）→ 停在 ready_for_review。

    停等语义：正常完成即置待审查（人工/AI 代审在评审页接管）；analyze 诚实降级
    （非深度支持语言）如实 failed；异常同 run_task 落 failed。不触碰容器。
    守卫全部走临时短连接（fail-closed 拒绝时零 Docker/注册表开销）。"""
    guard = get_conn()
    task = _load_task_full(guard, task_id)
    if task["confirmed"] != 1:
        guard.close()
        raise RuntimeError("任务未确认（confirmed=0），禁止启动阶段一 —— fail-closed 确认门")
    if task["status"] == "cancelled":
        guard.close()
        raise RuntimeError("任务已取消，禁止执行（A4 取消竞态守卫）——如需重跑请重新下发")
    # A4 同款乐观锁：执行链路中拒绝并发；准备阶段可从 queued/failed/ready_for_review 重启
    # （A4 2026-11-20：排除表补 cancelled——修复前已取消任务可被锁放行=取消后复活）
    lock = guard.execute(
        """UPDATE test_tasks SET status='cloning', updated_at=datetime('now')
           WHERE id=? AND status NOT IN ('cloning','analyzing','provisioning',
                                         'generating','running','reporting','cancelled')""", (task_id,))
    guard.commit()
    if lock.rowcount == 0:
        guard.close()
        raise RuntimeError("任务正在执行中，禁止并发下发")
    guard.close()

    deps = make_deps()
    conn = deps.conn
    session_id = _new_session(conn, task_id)
    init: TaskState = {
        "task_id": task_id,
        "session_id": session_id,
        "repo_url": task["repo_url"],
        "requirement": task["requirement_text"] or "",
        "container_name": f"mta-run-{task_id}",
        "round_no": 0,
        "coverage_target": task["coverage_target"] or config.COVERAGE_TARGET,
        "fingerprints": [],
        "auto_prepare": bool(task["auto_prepare"]) if "auto_prepare" in task.keys() else False,
    }
    graph = build_prep_graph(deps)
    try:
        final = graph.invoke(init, {"recursion_limit": 30})
    except Exception as e:
        _fail_task(conn, task_id, f"{type(e).__name__}: {e}")
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=? AND ended_at IS NULL",
            (session_id,))
        conn.commit()
        raise
    if final.get("done") and final.get("verdict") not in ("pass", "stop"):
        # 诚实降级短路（无 verdict 形态）：准备阶段无报告可出，如实失败（不做伪装的"待审查"）。
        # 注意 prep 图内唯一 done 生产者是 static_draft（verdict=pass/stop，草稿/UI 结果
        # 已落库）——2026-11-15 闲时找bug：旧代码把静态草稿的成功当降级误标 failed，
        # 两阶段 static_only 项目评审页永不可达（与图注释"静态模式停在待审查"矛盾）
        _fail_task(conn, task_id, final.get("stop_reason") or "准备阶段诚实降级终止")
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=? AND ended_at IS NULL",
            (session_id,))
        conn.commit()
    else:
        conn.execute("UPDATE test_tasks SET status='ready_for_review', error=NULL, updated_at=datetime('now') WHERE id=?",
                     (task_id,))   # error 一并清空（348 实锤：重跑成功后界面仍显示上一轮失败原因）
        conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?", (session_id,))
        # 空集停等要给用户自愈指引（E2E 实锤：超长提示词可致两轮出题均不可解析，
        # 此时评审页对话框增补就是补救通道——指出来，别让人对着空清单发呆）
        n_cases = conn.execute(
            """SELECT COUNT(*) AS n FROM test_cases c JOIN test_scripts sc ON sc.id=c.script_id
               WHERE sc.task_id=? AND c.source='ai' AND c.enabled=1""", (task_id,)).fetchone()["n"]
        if n_cases == 0:
            # 2026-11-09：空集提示按 generate 节点透传的实际 reason 归因（B 项），
            # 不再硬编码"两次不可解析"的猜测
            _reviewer_msg(conn, task_id, _empty_set_message(final.get("gen_reason")))
        conn.commit()
    return final


def run_task_exec(task_id: int) -> dict:
    """两阶段·阶段二入口：待审查停等点放行 → 执行图（布考场/开考/判卷/报告/复审）。

    首轮 round_no=1：run 节点跑全目录基线（自带 tests + 审查通过的生成用例集），
    与单阶段首轮口径一致；R-Loop/评估/复审完整保留在阶段二。
    守卫全部走临时短连接（fail-closed 拒绝时零 Docker/注册表开销）。"""
    guard = get_conn()
    task = _load_task_full(guard, task_id)
    if task["confirmed"] != 1:
        guard.close()
        raise RuntimeError("任务未确认（confirmed=0），禁止下发执行 —— fail-closed 确认门")
    if task["two_stage"] != 1:
        guard.close()
        raise RuntimeError("任务未开启两阶段流程，请走单阶段下发")
    # 放行锁：仅待审查态可启动阶段二（准备阶段未完成 = 没有可执行的用例集）。
    # 2026-11-15 状态机四断点：api_run 放行即置 queued（worker 排队期间状态可见），
    # 锁同步放行 queued——否则 worker 取行时锁失败，任务永远启动不了。
    lock = guard.execute(
        """UPDATE test_tasks SET status='provisioning', updated_at=datetime('now')
           WHERE id=? AND status IN ('ready_for_review','queued')""", (task_id,))
    guard.commit()
    if lock.rowcount == 0:
        guard.close()
        raise RuntimeError("任务不在待审查状态，禁止启动阶段二")
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        _fail_task(guard, task_id, "工作区不存在（阶段一未完成或已被清理）")
        guard.close()
        raise RuntimeError("工作区不存在：阶段一未完成，禁止启动阶段二")
    guard.close()

    deps = make_deps()
    conn = deps.conn
    try:
        deps.runner.cleanup(f"mta-run-{task_id}")
    except Exception as e:
        log.warning("预清理容器失败: %s", e)
    session_id = _new_session(conn, task_id)
    primary = task["primary_language"] or "python"
    module_row = conn.execute("SELECT id FROM modules WHERE project_id=? AND path='.'",
                              (task["project_id"],)).fetchone()
    init: TaskState = {
        "task_id": task_id,
        "session_id": session_id,
        "repo_url": task["repo_url"],
        "requirement": task["requirement_text"] or "",
        "container_name": f"mta-run-{task_id}",
        "round_no": 1,
        "coverage_target": task["coverage_target"] or config.COVERAGE_TARGET,
        "fingerprints": [],
        "workspace": str(ws),
        "primary_language": primary,
        "image": config.EXEC_LANGUAGES.get(primary, config.IMAGE_PY),
        "module_id": module_row["id"] if module_row else None,
        "auto_prepare": bool(task["auto_prepare"]) if "auto_prepare" in task.keys() else False,
        "two_stage_rebuild": True,
    }
    # A2 验证冻结（2026-11-20）：verify_mode 贯穿任务行 → TaskState → 图路由；
    # 用例集指纹执行前后留痕 agent_messages = 审计证据（写入拦截靠图路由，不靠指纹）
    is_verify = bool(task["verify_mode"])
    fp_base = None
    if is_verify:
        from ..skills.verify_fix import case_set_fingerprint
        fp_base = case_set_fingerprint(conn, task_id)
        init["verify_mode"] = 1
        _reviewer_msg(conn, task_id,
                      f"[verify-freeze] 冻结基线：用例集指纹 {fp_base}"
                      "（生成/剪除/重写/回炉已被图级拦截，A2）")
    graph = build_exec_graph(deps)
    try:
        final = graph.invoke(init, {"recursion_limit": 60})
        if is_verify:
            fp_final = case_set_fingerprint(conn, task_id)
            _reviewer_msg(conn, task_id,
                          f"[verify-freeze] 终态指纹 {fp_final} —— "
                          + ("一致：用例集全程未被改动" if fp_final == fp_base
                             else "不一致：用例集被改动（A2 审计告警，须人工核查）"))
        return final
    except Exception as e:
        if is_verify:
            try:
                fp_final = case_set_fingerprint(conn, task_id)
                _reviewer_msg(conn, task_id,
                              f"[verify-freeze] 异常路径指纹 {fp_final} —— "
                              + ("一致" if fp_final == fp_base else "不一致（审计告警）"))
            except Exception:  # noqa: BLE001 —— 审计留痕失败不掩盖原异常
                pass
        _fail_task(conn, task_id, f"{type(e).__name__}: {e}")
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=? AND ended_at IS NULL",
            (session_id,))
        conn.commit()
        raise
    finally:
        try:
            deps.runner.cleanup(f"mta-run-{task_id}")
        except Exception as e:
            log.warning("容器回收失败: %s", e)
        try:
            deps.runner.cleanup(f"mta-dbsidecar-{task_id}")   # 卡 6：sidecar 任务级回收
        except Exception as e:
            log.warning("sidecar 回收失败: %s", e)


def log_feedback_message(conn, task_id: int, fb_no: int, feedback_text: str) -> int | None:
    """C（任务卡-369 20261105）反馈原文留档：369 的 expected_basis=「用户反馈」用例
    无法溯源（反馈原文只在内存传递）——写 agent_messages（role=user）留档，basis
    可软链回查。session 不可得时返回 None（不阻断增补主链路）。"""
    try:
        ses = conn.execute(
            "SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        if ses is None:
            return None
        seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                           (ses["id"],)).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
            (ses["id"], seq, "user", f"[feedback 第{fb_no}轮] {feedback_text}"))
        conn.commit()
        return cur.lastrowid
    except Exception as e:      # noqa: BLE001 —— 留痕失败不炸增补主链路（如实日志）
        logging.warning("反馈留档失败 task=%s: %s", task_id, e)
        return None


def run_feedback_round(task_id: int, feedback_text: str, runner=None) -> dict:
    """两阶段③·对话增补共用管线（人工对话框与 AI 代审 regenerate 走同一端点语义）：
    反馈进提示词 → 定向增补生成（独立 test_gen_fbN.py 走同一三道闸，不动已批准用例）→
    落库（llm_calls phase=gen_feedback）。预算封顶 FEEDBACK_MAX_ROUNDS 轮；
    仅待审查态可调（fail-closed：状态/确认/预算/Key 任何一关不过即拒绝）。"""
    feedback_text = (feedback_text or "").strip()
    if not feedback_text:
        raise ValueError("反馈文本不能为空")
    conn = get_conn()
    task = _load_task_full(conn, task_id)
    if task["status"] != "ready_for_review":
        raise RuntimeError(f"任务当前状态为「{task['status']}」，仅待审查状态可增补用例")
    if task["confirmed"] != 1:
        raise RuntimeError("任务未确认，禁止增补（fail-closed 确认门）")
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        raise RuntimeError("工作区不存在（阶段一未完成）")
    fb_no = len(list(ws.glob("test_gen_fb*.py"))) + 1
    log_feedback_message(conn, task_id, fb_no, feedback_text)
    if fb_no > config.FEEDBACK_MAX_ROUNDS:
        raise RuntimeError(f"增补预算已用尽（上限 {config.FEEDBACK_MAX_ROUNDS} 轮），"
                           f"请整理用例集后下发执行")
    from ..skills.llm_profile import current as _llm_current, effective_key, missing_key_message
    api_key = effective_key()   # 2026-11-09：统一 key 解析（local 档恒 ollama，不再误判无 Key）
    if not api_key:
        raise RuntimeError(missing_key_message(_llm_current()) + "，无法增补生成（诚实降级）")
    module_row = conn.execute("SELECT id FROM modules WHERE project_id=? AND path='.'",
                              (task["project_id"],)).fetchone()
    existing_names = [r["name"] for r in conn.execute(
        """SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=?""", (task_id,))]
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 100) + 1 AS v FROM test_scripts "
        "WHERE task_id=? AND framework='pytest'", (task_id,)).fetchone()
    from ..skills.gen_tests import generate_tests
    if runner is None:
        try:
            from ..sandbox.runner import SandboxRunner
            runner = SandboxRunner()
        except Exception as e:  # noqa: BLE001 —— Docker 未就绪：出题不需容器（两阶段本意），
            # collect 闸/回灌在 gen_tests 内诚实顺延到布考场；这里只保证 runner 可传
            log.warning("feedback 轮 Docker 未就绪（%s: %s），collect 闸顺延", type(e).__name__, e)

            class _DeferredRunner:
                client = None

                def exec_in(self, *_a, **_k):
                    raise RuntimeError("Docker 未就绪")

            runner = _DeferredRunner()
    summary = generate_tests(api_key, {
        "workspace": str(ws),
        "requirement": task["requirement_text"] or "",
        "module_hint": task["primary_language"] or "",
        "container_name": f"mta-run-{task_id}",
        "module_id": module_row["id"] if module_row else None,
        "round_no": 1,
        "feedback": feedback_text,
        "fb_no": fb_no,
        "version": version_row["v"],
        "existing_names": existing_names,
    }, task_id, runner)
    return json.loads(summary)


# ==================== AI 代审 auto 模式（docs/AI代审设计-20261031.md） ====================

def post_prep_review_hook(task_id: int) -> None:
    """阶段一停等点钩子（B1 队列化后从 server._prep_worker 迁入——晚间实验实锤：
    队列 worker 只跑 prep 丢钩子，auto 任务停在待审查无人接管）。
    off→停等人工；hybrid→AI 先审一轮（决策落库）停等抽检；
    auto→D2 代审循环→收敛后自动入队阶段二（保持单 worker 串行纪律）。"""
    conn = get_conn()
    row = conn.execute("SELECT ai_reviewer, status FROM test_tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["status"] != "ready_for_review":
        return
    mode = row["ai_reviewer"]
    if mode == "hybrid":
        try:
            from ..skills.ai_reviewer import reviewer_llm, run_review_once
            summary = run_review_once(reviewer_llm(), conn, task_id, round_no=1)
            if summary:
                log.info("hybrid 代审完成 task=%s: %s", task_id,
                         {k: summary[k] for k in ("approved", "deleted", "regenerate", "inserted")})
        except Exception as e:  # noqa: BLE001 —— 代审失败=无决策落库，人工照常审查
            log.warning("hybrid 代审失败 task=%s: %s: %s", task_id, type(e).__name__, e)
    elif mode == "auto":
        from types import SimpleNamespace
        from ..sandbox.runner import SandboxRunner
        deps_conn = get_conn()   # 2026-11-08 锁循环实锤：D2 循环连接必须显式 close
        try:                     # （原实现丢弃引用=悬挂连接带事务锁全进程）
            deps = SimpleNamespace(conn=deps_conn, runner=SandboxRunner())
            # A1 放行门：只凭循环返回的 outcome 放行——fail-closed 分支终态同为
            # ready_for_review，任务状态区分不了"收敛"与"交人工"（2026-11-20 体检论断 1）
            loop_result = _auto_review_loop(deps, task_id)
            if (loop_result.get("outcome") == "converged"
                    and conn.execute("SELECT status FROM test_tasks WHERE id=?",
                                     (task_id,)).fetchone()["status"] == "ready_for_review"):
                from ..store.queue import QueueWorker   # 函数内延迟 import 防循环
                QueueWorker.enqueue(task_id, "exec")
                _reviewer_msg(conn, task_id, "[ai-review] D2 收敛，已自动入队阶段二")
        finally:
            deps_conn.close()


def _reviewer_msg(conn, task_id: int, content: str) -> None:
    """代审轨迹写进最新会话日志（前端日志流可见——AI 的思考过程对人可见）。"""
    row = conn.execute("SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if not row:
        return
    seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                       (row["id"],)).fetchone()["n"]
    conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
                 (row["id"], seq, "assistant", content))
    conn.commit()


def _auto_review_loop(deps: GraphDeps, task_id: int) -> dict:
    """auto 模式 D2 代审循环：审查 →（有 regenerate/insert 决策）→ 增补 → 再审 →
    收敛或预算（AI_REVIEWER_MAX_ROUNDS 轮 / token 停车线）耗尽。
    fail-closed：任何失败/不可解析/超线分支都停在 ready_for_review 交人工。

    返回 {"outcome": "converged"|"fail_closed", "reason": str}（A1 放行门，2026-11-20
    Qoder 体检论断 1：fail-closed 终态与收敛终态同为 ready_for_review，状态不可区分——
    调用方必须凭本返回值决定是否放行，不得再拿任务状态猜审查结论）。"""
    from ..skills.ai_reviewer import reviewer_llm, run_review_once, token_budget_exceeded
    conn = deps.conn
    llm = reviewer_llm()
    for round_no in range(1, config.AI_REVIEWER_MAX_ROUNDS + 1):
        if token_budget_exceeded(conn, task_id):
            _reviewer_msg(conn, task_id, "[ai-review] token 停车线触顶，停在待审查交人工（fail-closed）")
            return {"outcome": "fail_closed", "reason": "token 停车线触顶"}
        try:
            summary = run_review_once(llm, conn, task_id, round_no=round_no)
        except Exception as e:  # noqa: BLE001 —— 代审自身失败绝不炸链路，交人工
            log.warning("D2 代审异常 task=%s round=%s: %s", task_id, round_no, e)
            try:
                conn.rollback()   # 2026-11-08 锁循环实锤：写段抛异常必须回滚（悬挂事务锁全进程）
            except Exception:     # noqa: BLE001
                pass
            _reviewer_msg(conn, task_id,
                          f"[ai-review] 代审调用失败（{type(e).__name__}），停在待审查交人工")
            return {"outcome": "fail_closed", "reason": f"代审调用失败（{type(e).__name__}）"}
        if summary is None:
            _reviewer_msg(conn, task_id, "[ai-review] 代审输出不可解析，停在待审查交人工（fail-closed）")
            return {"outcome": "fail_closed", "reason": "代审输出不可解析"}
        fbs = [t for t in (summary.get("feedback_texts") or []) if t]
        if not fbs:
            _reviewer_msg(conn, task_id,
                          f"[ai-review] 第{round_no}轮代审收敛：通过{summary.get('approved', 0)}"
                          f" 删除{summary.get('deleted', 0)} 增补{summary.get('inserted', 0)}，放行阶段二")
            return {"outcome": "converged", "reason": f"第{round_no}轮代审收敛"}
        text = "；".join(fbs)[:1500]
        try:
            fb = run_feedback_round(task_id, text, runner=deps.runner)
        except Exception as e:  # noqa: BLE001 —— 增补失败不自动放行
            _reviewer_msg(conn, task_id,
                          f"[ai-review] 增补失败（{type(e).__name__}: {e}），停在待审查交人工")
            return {"outcome": "fail_closed", "reason": f"增补失败（{type(e).__name__}）"}
        _reviewer_msg(conn, task_id,
                      f"[ai-review] 第{round_no}轮按决策增补 {fb.get('generated', 0)} 条"
                      f"（反馈：{text[:120]}）")
        if not fb.get("generated"):
            _reviewer_msg(conn, task_id, "[ai-review] 增补 0 条（闸门剔除或不可解析），停在待审查交人工")
            return {"outcome": "fail_closed", "reason": "增补 0 条（闸门剔除或不可解析）"}
    _reviewer_msg(conn, task_id, "[ai-review] 代审轮次预算耗尽，停在待审查交人工（fail-closed）")
    return {"outcome": "fail_closed", "reason": "代审轮次预算耗尽"}


def run_task_auto(task_id: int) -> dict:
    """AI 代审 auto 全链（批量入口）：D1 准入代审（意见书+带理由确认）→ 阶段一 →
    D2 用例集代审循环 → 阶段二。人工已确认的任务跳过 D1（门由人满足，D2 仍代审）。
    守卫走临时短连接（未开 auto 的任务拒绝时零 Docker/注册表开销）。"""
    from ..skills.ai_reviewer import reviewer_llm, run_d1_review
    guard = get_conn()
    task = _load_task_full(guard, task_id)
    if task["ai_reviewer"] != "auto":
        guard.close()
        raise RuntimeError("任务未开启 AI 代审 auto 模式，禁止走代审链")
    confirmed_by_human = task["confirmed"] == 1
    guard.close()

    deps = make_deps()
    conn = deps.conn
    if confirmed_by_human:
        run_task_prep(task_id)          # 人满足 D1；阶段一跑完停在待审查，D2 交给代审
    else:
        session_id = _new_session(conn, task_id)
        d1 = None
        try:
            d1 = run_d1_review(reviewer_llm(), conn, task_id,
                               task["requirement_text"] or "", task["repo_url"])
        except Exception as e:  # noqa: BLE001
            log.warning("D1 代审失败 task=%s: %s", task_id, e)
        if d1 is None:
            _reviewer_msg(conn, task_id, "[ai-review] D1 准入代审不可用，停在待确认交人工（fail-closed）")
            conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?", (session_id,))
            conn.commit()
            return {"task_id": task_id, "stage": "D1", "stopped": "fail_closed"}
        if d1["decision"] != "approve":
            conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?", (session_id,))
            conn.commit()
            return {"task_id": task_id, "stage": "D1", "stopped": "rejected"}
        conn.execute(
            "UPDATE test_tasks SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
            (f"agent:reviewer-{session_id}", task_id))
        conn.commit()
        # D1 会话收口（2026-11-15 闲时找bug）：拒绝/失败路径都关了，approve 路径漏关
        # ——prep 会开新会话，D1 会话永久悬开
        conn.execute(
            "UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=? AND ended_at IS NULL",
            (session_id,))
        conn.commit()
        run_task_prep(task_id)
    # A1 放行门：只凭 outcome 放行（状态检查保留为双保险）
    loop_result = _auto_review_loop(deps, task_id)
    if loop_result.get("outcome") != "converged":
        return {"task_id": task_id, "stage": "D2", "stopped": "fail_closed"}
    row = conn.execute("SELECT status FROM test_tasks WHERE id=?", (task_id,)).fetchone()
    if row["status"] != "ready_for_review":
        return {"task_id": task_id, "stage": "D2", "stopped": "fail_closed"}
    return run_task_exec(task_id)
