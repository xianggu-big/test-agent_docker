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
from ..skills.builtin import build_skills
from ..skills.gen_tests import prune_failing_cases
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
    return GraphDeps(conn=conn, runner=runner,
                     registry=SkillRegistry(conn), skills=build_skills(runner))


def set_status(conn, task_id: int, status: str) -> None:
    conn.execute("UPDATE test_tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                 (status, task_id))
    conn.commit()


_ROW = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+([\d,\s\-]+)$")


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
        if strategy != "full":
            return {**out, "done": True, "verdict": "stop",
                    "stop_reason": f"主语言 {primary} 不在深度支持范围，v1 诚实降级（Tier2 静态扫描属后续工作）"}
        return out

    # ---------- 3. 环境供给（容器 + 快照 + 依赖安装，全部留台账） ----------
    def provision(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "provisioning")
        # 上一轮执行超时（exit=-1）说明容器里有 exec 残留，必须重建（A2），否则新旧任务争抢 CPU
        fresh = state.get("last_exit") == -1 and state.get("round_no", 0) > 0
        container = runner.ensure_container(state["image"], state["container_name"], fresh=fresh)
        runner.copy_dir_into(container, state["workspace"])
        dep = registry.invoke(tid, skills["install_deps"], {
            "container_name": state["container_name"],
            "has_requirements": (Path(state["workspace"]) / "requirements.txt").exists()})
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

    # ---------- 4. 生成用例（LLM，未配 Key 时诚实降级；repair_gen 先定向剪枝） ----------
    def generate(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "generating")
        next_round = state.get("round_no", 0) + 1

        # ②定向修复：上一轮用例自身坏掉的（ImportError 等强信号）先从工作区剪除；
        # 断言失败一律保留为疑似缺陷证人（§7.2 防造假），仓库自带测试不动。
        if state.get("round_no", 0) >= 1:
            pairs = [(nid, repair.error_class_of(state.get("last_stdout", ""), nid))
                     for nid in repair.failing_nodeids(state.get("last_stdout", ""))]
            if pairs:
                info = prune_failing_cases(Path(state["workspace"]), pairs, tid, conn)
                msg(state["session_id"], "assistant",
                    f"[定向修复] 剪除 {len(info['pruned'])} 条坏用例、"
                    f"保留证人 {len(info['kept'])} 条：" +
                    json.dumps(info, ensure_ascii=False)[:400])

        res = registry.invoke(tid, skills["gen_tests"], {
            "workspace": state["workspace"],
            "requirement": state.get("requirement", ""),
            "module_hint": state.get("primary_language", ""),
            "container_name": state["container_name"],
            "module_id": state.get("module_id"),
            "coverage_gaps": state.get("per_file") or [],   # ①覆盖缺口回灌（CoverUp 模式）
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

        fp = repair.fingerprint_of(stdout, stderr)
        verdict, kind, reason = repair.decide(
            state.get("last_exit", 1), stdout, stderr,
            state.get("fingerprints", []), fp,
            state.get("round_no", 0), coverage,
            state.get("coverage_target"))
        msg(state["session_id"], "assistant",
            f"[evaluate 第{state.get('round_no', 0)}轮] 通过{passed} 失败{failed} 跳过{skipped} "
            f"覆盖率{coverage if coverage is not None else 'N/A'} 指纹{fp[:8] or '无'} → {verdict}（{reason}）")
        return {"passed": passed, "failed": failed, "skipped": skipped,
                "coverage_line": coverage, "per_file": _parse_per_file_coverage(stdout),
                "fingerprint": fp, "failure_kind": kind,
                "verdict": verdict, "stop_reason": reason,
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

    return {"clone": clone, "analyze": analyze, "provision": provision,
            "generate": generate, "run": run, "evaluate": evaluate, "report": report}
