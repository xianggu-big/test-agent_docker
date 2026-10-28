"""LangGraph 节点：clone → analyze → provision → generate → run → evaluate → report。

约定：
- 节点进入时先落 test_tasks.status（界面进度条与状态机一一对应）
- 关键动作全部经 SkillRegistry.invoke（审计台账 + 确认门）
- 容器里只跑测试；归因判读（Evaluator）在宿主侧 —— 执行与判定分离
"""
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..sandbox.runner import SandboxRunner
from ..skills.builtin import build_skills, probe_deps_manifest
from ..skills.evaluate import build_eval_skills
from ..skills.repair_strategy import make_deepseek_fn, run_repair_strategy
from ..skills.prompt_library import PLAN_SYSTEM, build_plan_prompt
from ..skills.registry import SkillRegistry
from ..store.db import get_conn
from . import repair
from .state import TaskState

log = logging.getLogger(__name__)

_EXT_LANG = {".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
             ".jsx": "javascript", ".tsx": "typescript", ".go": "go", ".c": "c", ".cpp": "cpp"}


@dataclass
class GraphDeps:
    conn: object
    runner: SandboxRunner
    registry: SkillRegistry
    skills: dict


def make_deps() -> GraphDeps:
    conn = get_conn()
    runner = SandboxRunner()
    registry = SkillRegistry(conn)
    # 注册表幂等 upsert（A13）：skills 表缺行会让审计台账的 INSERT..SELECT 插 0 行，
    # 论文"工具调用成功率"指标就只剩 run_tests 一个工具——全部 skills 必须注册
    skills = {**build_skills(runner), **build_eval_skills()}
    for s in skills.values():
        registry.register(s)
    return GraphDeps(conn=conn, runner=runner, registry=registry, skills=skills)


def set_status(conn, task_id: int, status: str) -> None:
    conn.execute("UPDATE test_tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                 (status, task_id))
    conn.commit()


_ROW = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+([\d,\s\-]+)$")


def parse_conditions(text: str) -> list[dict] | None:
    """从 LLM 回复中鲁棒提取测试条件；解析失败返回 None（诚实降级，不硬造条件）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    conds = data.get("conditions")
    if not isinstance(conds, list):
        return None
    cleaned = [c for c in conds if isinstance(c, dict) and c.get("condition")]
    return cleaned or None


def _parse_per_file_coverage(stdout: str) -> list[dict]:
    """term-missing 逐文件覆盖明细（B2）：只留有缺口的文件行，供缺口回灌与趋势图。"""
    rows = []
    for line in stdout.splitlines():
        m = _ROW.match(line.strip())
        if m and m.group(1) not in ("Name", "TOTAL"):
            rows.append({"file": m.group(1), "stmts": int(m.group(2)),
                         "miss": int(m.group(3)), "cover": int(m.group(4)),
                         "missing": m.group(5).strip()})
    return rows


def prepare_task_image(runner, image: str, auto: bool) -> tuple[bool, str]:
    """沙箱自适应的镜像门（模块级纯函数便于单测）：

    auto=True（模式③一条龙 / AUTO_PREPARE_ENV / 任务勾选）→ 缺镜像自动构建（ensure_image）；
    auto=False（模式①手动感知）→ 只探测不构建，缺失时返回 False 由 provision 诚实 stop。
    返回 (是否就绪, 来源说明)。
    """
    if auto:
        if runner.ensure_image(image):
            return True, "本地已有或已自动构建"
        return False, "自动构建失败（详见服务端日志）"
    if runner.image_exists(image):
        return True, "本地已有"
    return False, "本地缺失（未开启自动准备）"


def make_nodes(deps: GraphDeps) -> dict:
    conn, runner = deps.conn, deps.runner
    registry, skills = deps.registry, deps.skills

    def msg(session_id: int, role: str, content: str) -> None:
        seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                           (session_id,)).fetchone()["n"]
        conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
                     (session_id, seq, role, content))
        conn.commit()

    # ---------- 1. 克隆 ----------
    def clone(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "cloning")
        ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        src = state["repo_url"]
        if os.path.isdir(src):
            shutil.copytree(src, ws, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*config.SKIP_DIRS))
            how = "本地目录快照"
        else:
            r = subprocess.run(["git", "clone", "--depth", "1", src, str(ws)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"git clone 失败: {r.stderr[:400]}")
            how = "git clone --depth 1"
        # P1-②：重跑 rmtree 后 imported 用例文件无人重建（DB 指标仍统计）——
        # 以库为准重放 test_imported.py，仅当库里确有 enabled=1 的 imported 用例
        from ..skills.gen_tests import _rebuild_imported_file
        n_imported = conn.execute(
            """SELECT COUNT(*) FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
               WHERE s.task_id=? AND c.source='imported' AND c.enabled=1""",
            (tid,)).fetchone()[0]
        if n_imported:
            _rebuild_imported_file(conn, tid, ws)
            msg(state["session_id"], "assistant",
                f"[clone] 从库重建 test_imported.py（imported 用例 {n_imported} 条，重跑语义恢复）")
        msg(state["session_id"], "assistant", f"[clone] {how} → {ws}")
        return {"workspace": str(ws)}

    # ---------- 2. 分析（语言占比 / 模块分级 / 准出标准冻结） ----------
    def analyze(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "analyzing")
        ws = Path(state["workspace"])
        pid = conn.execute("SELECT project_id FROM test_tasks WHERE id=?", (tid,)).fetchone()["project_id"]
        counts: dict[str, int] = {}
        for p in ws.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _EXT_LANG:
                continue
            if any(part in config.SKIP_DIRS for part in p.parts):
                continue
            try:
                lang = _EXT_LANG[p.suffix.lower()]
                counts[lang] = counts.get(lang, 0) + sum(1 for _ in p.open(errors="ignore"))
            except OSError:
                continue
        total = sum(counts.values()) or 1
        ratios = {k: round(v / total, 3) for k, v in counts.items()}
        primary = max(ratios, key=ratios.get) if ratios else "unknown"

        conn.execute("UPDATE projects SET primary_language=?, language_ratios=?, clone_status='ok' WHERE id=?",
                     (primary, json.dumps(ratios, ensure_ascii=False), pid))
        strategy = "full" if primary in ("python", "java") else "static_only"
        # upsert：A4 允许重跑 done/failed 任务，重跑时模块行必须幂等（否则 UNIQUE 崩）
        cur = conn.execute(
            """INSERT INTO modules(project_id, path, language, loc, strategy) VALUES (?, '.', ?, ?, ?)
               ON CONFLICT(project_id, path) DO UPDATE SET language=excluded.language,
                 loc=excluded.loc, strategy=excluded.strategy""",
            (pid, primary, total, strategy))
        # 准出标准冻结进任务行（A1）：此后 decide 只认这个值，实验中途改 config 不影响在途任务
        conn.execute("UPDATE test_tasks SET coverage_target=? WHERE id=?",
                     (config.COVERAGE_TARGET, tid))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[analyze] 语言占比 {ratios} → 主语言 {primary}，分级 {strategy}，"
            f"准出标准冻结：行覆盖率 ≥ {config.COVERAGE_TARGET:.0%} 或预算耗尽")
        out = {"primary_language": primary, "module_id": cur.lastrowid,
               "coverage_target": config.COVERAGE_TARGET,
               "image": config.IMAGE_PY if primary == "python" else config.IMAGE_JAVA}
        # W4 圈复杂度热点（radon 宿主侧确定性扫描）：注入 plan 提示词做风险分级倾斜
        if primary == "python":
            from ..skills.complexity import complexity_hotspots
            hotspots = complexity_hotspots(ws)
            if hotspots:
                out["complexity_hotspots"] = hotspots
                msg(state["session_id"], "assistant",
                    "[analyze] W4 复杂度热点：" +
                    "; ".join(f"{h['file']}::{h['name']}={h['complexity']}({h['rank']})"
                              for h in hotspots)[:300])
        if strategy != "full":
            return {**out, "done": True, "verdict": "stop",
                    "stop_reason": f"主语言 {primary} 不在深度支持范围，v1 诚实降级（Tier2 静态扫描属后续工作）"}
        return out

    # ---------- 3. 环境供给（容器 + 快照 + 依赖安装，全部留台账） ----------
    def provision(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "provisioning")
        # 沙箱自适应（设计文档 §6）：镜像按需供给——任务级判定，缺镜像按模式分流
        auto = config.AUTO_PREPARE_ENV or bool(state.get("auto_prepare"))
        ready, how = prepare_task_image(runner, state["image"], auto)
        if not ready:
            reason = ("执行镜像未就绪且未开启自动准备（模式①手动感知）："
                      "请在界面点「一键准备」或到设置页构建镜像，"
                      "或新建任务时勾选「自动准备环境」后重跑")
            msg(state["session_id"], "assistant", f"[provision] {reason}")
            return {"done": True, "verdict": "stop", "stop_reason": reason}
        msg(state["session_id"], "assistant", f"[provision] 执行镜像 {state['image']} 就绪（{how}）")
        # 上一轮执行超时（exit=-1）说明容器里有 exec 残留，必须重建（A2），否则新旧任务争抢 CPU
        fresh = state.get("last_exit") == -1 and state.get("round_no", 0) > 0
        container = runner.ensure_container(state["image"], state["container_name"], fresh=fresh)
        runner.copy_dir_into(container, state["workspace"])
        dep = registry.invoke(tid, skills["install_deps"], {
            "container_name": state["container_name"],
            "deps_manifest": probe_deps_manifest(state["workspace"])})
        cur = conn.execute(
            """INSERT INTO container_jobs(task_id, module_id, image, status, cpu_limit, mem_limit_mb,
                                          priority, timeout_at, container_id, started_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?, datetime('now', '+' || ? || ' seconds'), ?, datetime('now'))""",
            (tid, state.get("module_id"), state["image"],
             config.DEFAULT_CPU_LIMIT, config.DEFAULT_MEM_LIMIT_MB,
             5, config.DEFAULT_JOB_TIMEOUT_S, container.id))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[provision] 容器 {state['container_name']} 就绪；依赖安装：{dep.status}")
        return {"job_id": cur.lastrowid}

    # ---------- 4. 生成用例（LLM，未配 Key 时诚实降级；repair_gen 先按策略定向修复） ----------
    def generate(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "generating")
        next_round = state.get("round_no", 0) + 1

        # ②定向修复策略分派（调研报告 §8 行2/行3）：
        # A=prune 剪除+全量重生成（基线）｜B=rewrite 带上下文重写（YATE）｜C=hybrid 合成
        # 共用铁律：断言失败证人永不剪永不重写（§7.2），仓库自带测试不动。
        skip_full_regen = False
        if state.get("round_no", 0) >= 1:
            pairs = [(nid, repair.error_class_of(state.get("last_stdout", ""), nid))
                     for nid in repair.failing_nodeids(state.get("last_stdout", ""))]
            if pairs:
                info = run_repair_strategy(
                    config.REPAIR_STRATEGY, Path(state["workspace"]), pairs, tid, conn,
                    round_no=next_round,
                    llm_fn=make_deepseek_fn(os.environ.get("DEEPSEEK_API_KEY")),
                    runner=runner, container_name=state["container_name"],
                    requirement=state.get("requirement", ""),
                    fail_summary=(state.get("last_stdout", "") + state.get("last_stderr", ""))[-1500:])
                skip_full_regen = info.get("skip_full_regen", False)
                msg(state["session_id"], "assistant",
                    f"[定向修复·{info.get('strategy', config.REPAIR_STRATEGY)}] "
                    f"剪{len(info.get('pruned', []))} 重写{len(info.get('rewritten', []))} "
                    f"保留证人{len(info.get('kept', []))}：" +
                    json.dumps({k: info[k] for k in ("pruned", "rewritten", "kept", "fallback")
                                if info.get(k)}, ensure_ascii=False)[:400])

        if skip_full_regen:
            msg(state["session_id"], "assistant",
                f"[generate 第{next_round}轮] 坏用例全部重写成功，跳过全量重生成（YATE 路线）")
            return {"round_no": next_round}

        res = registry.invoke(tid, skills["gen_tests"], {
            "workspace": state["workspace"],
            "requirement": state.get("requirement", ""),
            "module_hint": state.get("primary_language", ""),
            "container_name": state["container_name"],
            "module_id": state.get("module_id"),
            "coverage_gaps": (state.get("per_file") or [])
                             if config.COVERAGE_FEEDBACK_ENABLED else [],  # ①缺口回灌（V4 开关）
            "coverage_target": state.get("coverage_target"),
            "round_no": next_round})
        msg(state["session_id"], "assistant", f"[generate 第{next_round}轮] {res.output[:400]}")
        return {"round_no": next_round}

    # ---------- 5. 执行（容器内 pytest，确认门已在注册表层把住） ----------
    def run(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "running")
        if state["primary_language"] != "python":
            # Java 链路占位：诚实终止，不进归因循环——"链路未实现"不该被伪装成环境失败（A7）
            return {"done": True, "verdict": "stop",
                    "stop_reason": "Java/Maven 执行链路 v1 未接入（诚实降级），任务终止"}
        cmd = ["pytest", "-q", "--tb=short", "-rfE",
               "--cov=.", "--cov-report=term-missing:skip-covered"]
        res = registry.invoke(tid, skills["run_tests"],
                              {"container_name": state["container_name"], "cmd": cmd})
        if res.status == "denied":
            raise RuntimeError("run_tests 被确认门拒绝：主流程不应在未确认时到达此节点")
        if res.status == "timeout":
            out = json.dumps({"exit": -1, "stdout": "", "stderr": res.error}, ensure_ascii=False)
        elif res.status != "ok":
            # skill 层失败（如容器瞬态消失/守护进程重启）：转成 env 失败走 repair_env 重建，
            # 而不是 json.loads 空输出把整个任务炸掉（实测踩坑：docker 守护进程中途重启）
            out = json.dumps({"exit": -1, "stdout": "",
                              "stderr": f"run_tests {res.status}: {res.error}"}, ensure_ascii=False)
        else:
            out = res.output
        r = json.loads(out)
        return {"last_exit": r["exit"], "last_stdout": r["stdout"], "last_stderr": r["stderr"]}

    # ---------- 6. 判读（Evaluator 在宿主侧：解析 + 归因 + R-Loop 判定） ----------
    def evaluate(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "reporting")
        stdout, stderr = state.get("last_stdout", ""), state.get("last_stderr", "")
        passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", stdout)) else 0
        failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", stdout)) else 0
        skipped = int(m.group(1)) if (m := re.search(r"(\d+) skipped", stdout)) else 0
        errors = int(m.group(1)) if (m := re.search(r"(\d+) errors?", stdout)) else 0
        failed += errors
        coverage = None
        if m := re.search(r"TOTAL.*?\s(\d+)%", stdout):
            coverage = int(m.group(1)) / 100

        # V8 证人冻结+继续补测：断言失败用例累积为冻结证人（当轮产生当轮生效），
        # 证人的失败指纹不参与熔断——有意保留≠原地打转；开关关时下面三处与
        # 既有口径逐字节一致（witness 恒空、fp 不排除、decide 不传证人）。
        witness = list(state.get("witness_nodeids") or [])
        if config.WITNESS_CONTINUE_ENABLED:
            for nid in repair.assertion_failure_nodeids(stdout):
                if nid not in witness:
                    witness.append(nid)
        fp = repair.fingerprint_of(
            stdout, stderr,
            exclude_nodeids=tuple(witness) if config.WITNESS_CONTINUE_ENABLED else ())
        verdict, kind, reason = repair.decide(
            state.get("last_exit", 1), stdout, stderr,
            state.get("fingerprints", []), fp,
            state.get("round_no", 0), coverage,
            state.get("coverage_target"),
            witness_nodeids=witness if config.WITNESS_CONTINUE_ENABLED else None)
        round_no = state.get("round_no", 0)
        # P1-③：每轮评估结构化落库（evaluation_rounds，轨迹唯一真源）——此前只写
        # 叙述消息，collect_metrics 靠正则解析散文取轨迹，措辞一变即静默清空。
        # 重跑再进图 round 从 1 重新计：delete-first 以本轮为准（与 plan 同幂等语义）
        if round_no <= 1:
            conn.execute("DELETE FROM evaluation_rounds WHERE task_id=?", (tid,))
        conn.execute(
            """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,
                                            coverage_line, verdict, failure_kind,
                                            stop_reason, fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, round_no, passed, failed, skipped, coverage, verdict, kind, reason, fp))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[evaluate 第{round_no}轮] 通过{passed} 失败{failed} 跳过{skipped} "
            f"覆盖率{coverage if coverage is not None else 'N/A'} 指纹{fp[:8] or '无'} → {verdict}（{reason}）")
        return {"passed": passed, "failed": failed, "skipped": skipped,
                "coverage_line": coverage, "per_file": _parse_per_file_coverage(stdout),
                "fingerprint": fp, "failure_kind": kind,
                "verdict": verdict, "stop_reason": reason,
                "witness_nodeids": witness,
                "fingerprints": state.get("fingerprints", []) + [fp]}

    # ---------- 7. 报告落库 + 诚实复盘 ----------
    def report(state: TaskState) -> dict:
        tid = state["task_id"]
        ok = state.get("verdict") == "pass"
        conn.execute(
            """INSERT INTO test_reports(task_id, module_id, passed, failed, skipped,
                                        coverage_line, defects, per_file, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, state.get("module_id"), state.get("passed", 0), state.get("failed", 0),
             state.get("skipped", 0), state.get("coverage_line"),
             json.dumps({"failure_kind": state.get("failure_kind"),
                         "stop_reason": state.get("stop_reason")}, ensure_ascii=False),
             json.dumps(state.get("per_file") or [], ensure_ascii=False), None))
        final = "done" if ok else "failed"
        conn.execute("UPDATE test_tasks SET status=?, error=?, updated_at=datetime('now') WHERE id=?",
                     (final, None if ok else state.get("stop_reason"), tid))
        conn.execute("UPDATE container_jobs SET status=?, finished_at=datetime('now') WHERE id=?",
                     ("done" if ok else "failed", state.get("job_id", 0)))
        conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?",
                     (state["session_id"],))
        conn.commit()
        digest = (f"[digest] 终态 {final}｜通过{state.get('passed', 0)} 失败{state.get('failed', 0)}"
                  f"｜覆盖率{state.get('coverage_line')}｜{state.get('stop_reason')}")
        msg(state["session_id"], "assistant", digest)
        return {"done": True, "stop_reason": state.get("stop_reason")}

    # ---------- 2.5 测试计划（RTM：需求 → 测试条件，测试分析阶段的产物） ----------
    def plan(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "analyzing")  # 测试条件制定属测试分析阶段（理论章口径）
        requirement = (state.get("requirement") or "").strip()
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not config.PLAN_ENABLED:   # 实验变量 V3：plan 对照组
            msg(state["session_id"], "assistant", "[plan] 实验开关关闭（对照组）：跳过测试条件拆解")
            return {}
        if not requirement or not api_key:
            why = "无需求文本" if not requirement else "未配置 DEEPSEEK_API_KEY"
            msg(state["session_id"], "assistant", f"[plan] {why}：跳过测试条件拆解（诚实降级）")
            return {}
        from langchain_openai import ChatOpenAI  # 延迟导入，无 Key 时零开销
        llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                         base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                         timeout=60, max_retries=2)
        resp = llm.invoke([("system", PLAN_SYSTEM),
                           ("user", build_plan_prompt(requirement,
                                                      hotspots=state.get("complexity_hotspots")))])
        conds = parse_conditions(resp.content)
        if not conds:
            msg(state["session_id"], "assistant", "[plan] 需求拆解不可解析，跳过（诚实降级）")
            return {}
        # delete-first：任务重跑会再次进图，裸 INSERT 会让条件翻倍（P1-①）；
        # 以本轮拆解为准整体替换，与 analyze 的 upsert 同为重跑幂等语义
        conn.execute("DELETE FROM test_conditions WHERE task_id=?", (tid,))
        for c in conds:
            conn.execute(
                """INSERT INTO test_conditions(task_id, requirement_ref, condition, priority, method_hint)
                   VALUES (?,?,?,?,?)""",
                (tid, c.get("requirement_ref"), c["condition"],
                 c.get("priority"), c.get("method_hint")))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[plan] 需求拆解为 {len(conds)} 条测试条件（RTM 落库）：" +
            "; ".join(f"[{c.get('priority') or '中'}]{c['condition'][:36]}"
                      for c in conds)[:400])
        return {}

    # ---------- 8. 评估阶段：五工具编排 + 报告二次审查（vision §1.6 双闸回炉） ----------
    def review(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "reporting")   # 评估属报告阶段；状态机 CHECK 枚举不扩（老库无 CHECK 迁移）
        verdict = state.get("verdict", "")
        ok = verdict == "pass"
        try:
            return _review_body(state, tid, verdict)
        finally:
            # 终态恢复：report 节点已写 done/failed，review 入口的 reporting 只是过程态；
            # 无论审查 pass/reject，任务终态跟随执行判定（审查结论落 test_reports.review，不改判任务）
            set_status(conn, tid, "done" if ok else "failed")

    def _review_body(state: TaskState, tid: int, verdict: str) -> dict:
        # 快照一致性：指标包只 collect 一次，gen_report / review_report 经 args 共用同一份——
        # 各自现 collect 会让"评估工具自身台账"在两次采集间漂移，LLM 闸永远可能自审自 reject
        res_m = registry.invoke(tid, skills["collect_metrics"], {})
        if res_m.status != "ok" or "no_report" in (res_m.output or "{}"):
            msg(state["session_id"], "assistant",
                "[review] 无报告数据（static_only/诚实短路链路），跳过评估五工具")
            return {}
        metrics = json.loads(res_m.output)

        res_r = registry.invoke(tid, skills["map_requirements"], {"use_llm": True})
        rtm = json.loads(res_r.output) if res_r.status == "ok" else {"matrix": []}
        registry.invoke(tid, skills["gen_report"], {"metrics": metrics, "rtm": rtm})
        msg(state["session_id"], "assistant",
            f"[review·collect] 通过{metrics['passed']} 失败{metrics['failed']}"
            f" 覆盖率{metrics['coverage_line']}｜工具成功率{metrics.get('tool_success_rate')}"
            f"｜六要素完整率{metrics.get('six_elem_rate')}")
        if res_r.status == "ok":
            msg(state["session_id"], "assistant",
                f"[review·rtm] 条件覆盖 {rtm['covered']}/{rtm['conditions_total']}"
                f"（缺口 {rtm['gap']}，映射方式 {rtm['source']}）"
                + (f"｜{rtm['note']}" if rtm.get("note") else ""))

        res_v = registry.invoke(tid, skills["review_report"],
                                {"metrics": metrics, "verdict": verdict})
        if res_v.status != "ok":
            msg(state["session_id"], "assistant",
                f"[review] 审查工具失败（{res_v.error[:120]}）：fail-open 放行，留痕备查")
            return {}
        rev = json.loads(res_v.output)
        msg(state["session_id"], "assistant",
            f"[review·审查] {rev['status']}（{rev['reviewer']}）"
            + ("；原因：" + "; ".join(rev["reasons"])[:300] if rev["reasons"] else ""))

        paths = []
        if res_r.status == "ok":
            # 单快照语义（run46 根修收官）：charts 也吃同一份 metrics/rtm 下传——
            # 叙述、审查、图表三类产物全部派生自 collect 一次的同一时点，彼此零漂移
            res_c = registry.invoke(tid, skills["gen_charts"], {"metrics": metrics, "rtm": rtm})
            if res_c.status == "ok":
                paths = json.loads(res_c.output).get("charts", [])
                if paths:
                    msg(state["session_id"], "assistant",
                        f"[review·charts] {len(paths)} 张图 → data/reports/charts/")

        # 回炉语义（vision §1.6）：stop（product_defect）是证人冻结的上报，审查只修叙述不改判。
        # 受控重写（快照单点根修后解锁）：reject → 以新快照重写报告一次 → 复审一次，
        # 无论复审结果如何一律收敛（硬上限 1 次、在节点内完成、不回测试环——run46 的
        # 14 轮死循环由构造排除：重写只改叙述，不改数据）。
        if rev["status"] == "reject":
            if state.get("review_rejects", 0) >= 1:
                return {"review_rejects": 1, "review_status": "rejected"}
            msg(state["session_id"], "assistant",
                "[review·重写] 首次 reject：以新快照受控重写报告一次（硬上限 1，复审后必收敛）")
            res_m2 = registry.invoke(tid, skills["collect_metrics"], {})
            if res_m2.status == "ok" and "no_report" not in (res_m2.output or "{}"):
                metrics2 = json.loads(res_m2.output)
                registry.invoke(tid, skills["gen_report"], {"metrics": metrics2, "rtm": rtm})
                res_v2 = registry.invoke(tid, skills["review_report"],
                                         {"metrics": metrics2, "verdict": verdict})
                rev2 = json.loads(res_v2.output) if res_v2.status == "ok" else {"status": "reject"}
                msg(state["session_id"], "assistant",
                    f"[review·复审] {rev2['status']}（受控重写后终审，无论结果收敛）")
                status = "pass" if rev2["status"] == "pass" else "rejected"
                return {"review_rejects": 1, "review_status": status}
            return {"review_rejects": 1, "review_status": "rejected"}
        return {"review_status": "pass"}

    return {"clone": clone, "analyze": analyze, "plan": plan, "provision": provision,
            "generate": generate, "run": run, "evaluate": evaluate, "report": report,
            "review": review}
