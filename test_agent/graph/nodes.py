"""LangGraph 节点：clone → analyze → provision → generate → run → evaluate → report。

约定：
- 节点进入时先落 test_tasks.status（界面进度条与状态机一一对应）
- 关键动作全部经 SkillRegistry.invoke（审计台账 + 确认门）
- 容器里只跑测试；归因判读（Evaluator）在宿主侧 —— 执行与判定分离
"""
import io
import json
import logging
import os
import re
import fnmatch
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..sandbox.runner import SandboxRunner
from ..skills.builtin import build_skills, probe_deps_manifest
from ..skills.defect_handoff import auto_export_handoff
from ..skills.evaluate import build_eval_skills
from ..skills.llm_usage import log_llm_call
from ..skills.repair_strategy import make_deepseek_fn, run_repair_strategy
from ..skills.prompt_library import PLAN_SYSTEM, build_plan_prompt
from ..skills.registry import SkillRegistry
from ..store.db import get_conn
from . import repair
from .state import TaskState

log = logging.getLogger(__name__)

_EXT_LANG = {".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
             ".jsx": "javascript", ".tsx": "typescript", ".go": "go", ".c": "c", ".cpp": "cpp"}

_GH_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def build_run_cmd(round_no: int = 1, gen_files: list[str] | None = None) -> list[str]:
    """L1 主图 pytest 命令（Loop-8 抽出供单测；Loop-9 参数化）。

    --continue-on-collection-errors（Loop-8/D10 后继，三角色实验 30 轮实锤）：tenacity 案例中
    仓库自带 tests/ 缺依赖（tornado）→ 收集中断『1 error』→ 5 条生成用例根本未执行即熔断。
    加该 flag 后收集错误单列（仍计入 failed 供归因，诚实性不变），其余测试正常执行。

    Loop-9（三角色实验 30 题复核实锤）：自带 tests 的**恒定失败**混进 R-Loop 后被"补测修复"
    （永远修不好→熔断/预算耗尽）。修复：round_no>1 起 cmd 限定生成物+imported 文件——
    R-Loop 的 passed/failed 只衡量生成用例质量；首轮保持全目录（自带测试基线）。"""
    cmd = ["pytest", "-q", "--tb=short", "-rfE", "--continue-on-collection-errors",
           "--cov=.", "--cov-report=term-missing:skip-covered",
           # 任务C：逐用例结果（junitxml→test_case_results 表，实时看板数据源）；
           # 与 stdout 解析互不影响（--junitxml 只写文件）；超时/崩溃无文件=诚实缺席
           f"--junitxml={config.JUNIT_PATH}"]
    if round_no > 1 and gen_files:
        cmd += gen_files
    return cmd


def build_run_cmd_js(round_no: int = 1, gen_files: list[str] | None = None) -> list[str]:
    """JS 执行链路（Tier2）jest 命令：--json 供宿主侧适配器转 pytest 文本，--coverage 取
    istanbul coverageMap；--ci 关交互 / 快照写入。首轮全目录（自带 tests 基线，同 Python
    口径）；round>1 位置参数限定生成物（jest 把位置参数当 testPathPattern）——Loop-9 同语义。
    --ignore-scripts 类安全：jest 不执行 package 脚本；依赖安装阶段已 --ignore-scripts。
    TS 同走此命令（白包阶段1）：ts-jest 输出仍是 jest JSON，js_adapter 零改动。"""
    cmd = ["jest", "--json", "--coverage", "--ci", "--silent", "--forceExit",
           "--testLocationInResults"]
    if round_no > 1 and gen_files:
        cmd += gen_files
    return cmd


def build_run_cmd_java() -> list[str]:
    """Java 执行链路（Tier2，白包阶段2）：mvn test 跑全项目（surefire+jacoco 由项目 pom 声明，
    依赖随项目走）。round>1 位置参数不适用（surefire 扫全 test 类）——R-Loop 限定生成物语义
    由 gen 侧保证（首轮 TestGenR1，补测轮 TestGenR2 独立类共存，自带 tests 第 2 轮起不隔离
    属已知边界：maven 无 jest testPathPattern 等价物，诚实入档）。"""
    return ["mvn", "test"]


def _tier_strategy(primary: str) -> str:
    """语言分级（analyze 用，Loop 抽出供单测）：full=容器执行链路；其余 static_only。
    typescript 升 Tier2（白包阶段1）；java 接线（白包阶段2）→ full。"""
    return "full" if primary in ("python", "javascript", "typescript", "java") else "static_only"


def _inline_jest_config(include_ts: bool) -> str:
    """散装项目内联 jest 配置（无 jest.config/package.json 时的兜底，853 实锤先例）。
    include_ts=True（白包阶段1）：testMatch 加 .test.ts + ts-jest transform——
    全局镜像已备 ts-jest（NODE_PATH 可达），散装 TS 不需要项目级配置即可执行。"""
    cfg: dict = {"testMatch": ["**/*.test.js"], "testEnvironment": "node",
                 "moduleFileExtensions": ["js", "json"]}
    if include_ts:
        cfg["testMatch"] = ["**/*.test.ts", "**/*.test.js"]
        cfg["moduleFileExtensions"] = ["ts", "js", "json"]
        cfg["transform"] = {"\\.[jt]sx?$": ["ts-jest", {"tsconfig": {"esModuleInterop": True}}]}
    return json.dumps(cfg)


def _pkg_declares_jest(ws: Path) -> bool:
    """package.json 是否内嵌 jest 配置键（config-as-package 形态，注入判定用）。
    必须 JSON 解析判顶层键——子串判定被 scripts 值误命中（ts-calc 真机实锤：
    "test": "jest" 含 "jest" 子串 → 误判有配置 → 不注入内联配置 → 全局 jest
    无配置拒跑 → suite 全挂）。解析失败按无配置处理（fail-open 到内联兜底）。"""
    p = ws / "package.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    return isinstance(data, dict) and "jest" in data


def _validate_repo_url(src: str) -> None:
    """Loop-6/TODO-13（体检红队四问降级项）：repo_url 白名单校验——git clone 的 src
    以位置参数传入，option 形态注入实测不执行但防御性收敛：只允许 http(s)/git@/ssh URL
    或本地存在路径；含空白/引号/Option 前缀（-）/shell 元字符一律 RuntimeError。"""
    if os.path.isdir(src):
        return
    if src.startswith("-") or re.search(r"[\s\"';&|`$<>]", src):
        raise RuntimeError(f"repo_url 非法（含空白/引号/shell 元字符或 Option 前缀）：{src[:80]}")
    if not re.match(r"^https?://|^git@|^ssh://", src):
        raise RuntimeError(f"repo_url 非法（仅支持 http(s)/git@/ssh URL 或本地路径）：{src[:80]}")


def _extract_tarball_strip1(data: bytes, dest: Path) -> int:
    """GitHub tarball（顶层 <repo>-<branch>/）解包到 dest，剥顶层目录；返回落盘文件数。

    逐成员手写落盘而非 extractall：要剥前缀，且只收常规文件、拒绝路径穿越。"""
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) < 2:
                continue
            target = dest.joinpath(*parts[1:]).resolve()
            if dest.resolve() not in target.parents:
                continue  # 路径穿越（../ 等）直接丢弃
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(member)
            if f is None:
                continue
            target.write_bytes(f.read())
            n += 1
    return n


def _codeload_fallback(src: str, ws: Path) -> str | None:
    """git clone 失败时的确定性回退：codeload tarball 直连下载（显式绕代理）。

    调研结论（2026-10-30）：GitPython/dulwich/pygit2 都是 git 协议实现，同样依赖
    网络/代理，没有现成轮子做"git→tarball 回退"，逻辑太贴场景不值得引重依赖。
    仅支持 GitHub 仓库 URL；main/master 双分支尝试；失败返回 None 由调用方如实报错。"""
    m = _GH_URL.match(src.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # 显式空 ProxyHandler：代理失效环境里 codeload 直连可达（2026-10-30 实测），
    # 不继承系统/环境代理配置
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for branch in ("main", "master"):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        try:
            with opener.open(urllib.request.Request(
                    url, headers={"User-Agent": "test-agent"}), timeout=120) as r:
                data = r.read()
            n = _extract_tarball_strip1(data, ws)
            if n:
                return f"codeload tarball（{branch} 分支，{n} 个文件）"
        except Exception as e:  # 网络/解包失败换下一分支，全败返回 None
            log.warning("codeload 回退失败 %s: %s", url, e)
            continue
    return None


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
    from .l2 import build_l2_skills
    skills = {**build_skills(runner), **build_eval_skills(), **build_l2_skills(runner)}
    from ..skills.static_scan import _static_scan_skill
    skills["static_scan"] = _static_scan_skill()
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


def _try_recipe_image(state, tid: int, runner, msg) -> str | None:
    """任务F：探测→确定性配方→构建+自检，成功返回镜像 tag，失败返回 None（回退预置镜像）。

    全程落 image_recipes 台账（配方原文/来源/阶段/错误）——供应链可审计。
    任何异常都只 WARN 不上抛：环境增强失败不得拖垮任务。
    """
    try:
        from ..skills.recipe import try_build_recipe
        from ..skills.stack_detect import detect_stack, deterministic_recipe
        ws = Path(state["workspace"])
        stack = detect_stack(ws)
        rec = deterministic_recipe(stack)
        if not rec:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 探测结果不足以生成配方"
                f"（语言={stack.get('language')} 版本={stack.get('runtime_version')}）→ 用预置镜像")
            return None
        if stack.get("conflicts"):
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 探测注意：{'；'.join(stack['conflicts'])}")
        res = try_build_recipe(rec, runner)
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO image_recipes(task_id, source, recipe, image_tag, status, stage, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid, rec.get("source", "deterministic"), json.dumps(rec, ensure_ascii=False),
                 res.get("tag"), "built" if res["ok"] else "failed",
                 res.get("stage"), (res.get("error") or "")[:800] or None))
            conn.commit()
        finally:
            conn.close()
        if res["ok"]:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 环境配方构建通过（{rec['language']} "
                f"{rec['runtime_version']} / {rec['package_manager']}）→ 镜像 {res['tag']}"
                f"；依据：{', '.join(rec['evidence']['detected_from'][:4]) or '—'}")
            return res["tag"]
        msg(state["session_id"], "assistant",
            f"[provision·recipe] 配方在 {res.get('stage')} 阶段失败，回退预置镜像："
            f"{(res.get('error') or '')[:200]}")
        return None
    except Exception as e:      # noqa: BLE001 —— 环境增强失败不拖垮任务
        log.warning("recipe 流程异常 task=%s: %s: %s", tid, type(e).__name__, e)
        try:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 流程异常（{type(e).__name__}），回退预置镜像")
        except Exception:       # noqa: BLE001
            pass
        return None


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
        _validate_repo_url(src)  # Loop-6/TODO-13：非法 URL/路径 fail-closed
        if os.path.isdir(src):
            # 本地路径：copytree 本身尊重源项目 .gitignore（晚间实锤 348：项目 .gitignore
            # 已排除 data/ 7.4GB，但此前的裸 copytree 照拷不误，clone 后预算检查又不尊重
            # .gitignore → 把用户按提示做了的事直接否定）——spec 命中目录/文件直接不复制
            from ..sandbox.runner import _load_gitignore_spec
            spec = _load_gitignore_spec(Path(src))
            src_root = Path(src).resolve()

            def _gi_ignore(directory, names):
                rel_base = Path(directory).resolve().relative_to(src_root)
                skip = []
                for n in names:
                    rel = (rel_base / n).as_posix()
                    is_dir = (Path(directory) / n).is_dir()
                    if any(fnmatch.fnmatch(n, pat) for pat in config.SKIP_DIRS):
                        skip.append(n)
                    elif spec is not None and spec.match_file(rel + ("/" if is_dir else "")):
                        skip.append(n)
                return skip

            shutil.copytree(src, ws, dirs_exist_ok=True, ignore=_gi_ignore)
            how = "本地目录快照（尊重 .gitignore）"
        else:
            r = subprocess.run(["git", "clone", "--depth", "1", src, str(ws)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                # D2（2026-10-30 H1 备料实锤）：git 走系统代理，代理失效时真实 GitHub URL
                # 必炸 env 类错误；codeload tarball 直连可达 → 确定性回退，失败原因如实留痕
                fb = _codeload_fallback(src, ws)
                if fb:
                    msg(state["session_id"], "assistant",
                        f"[clone] git clone 失败（{r.stderr.strip()[-160:]}），回退 {fb}")
                    how = fb
                else:
                    raise RuntimeError(
                        f"git clone 失败且无可用回退（非 GitHub 仓库或分支不可达）: {r.stderr[:400]}")
            else:
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
        # B7 clone 预算：工作区体积超限 fail-fast（不让巨型仓库把磁盘拖满后
        # 在后续阶段以各种奇怪方式失败）；阈值复用任务B 的快照预算
        ws_bytes = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())
        from ..sandbox.runner import _fmt_bytes
        if ws_bytes > config.SNAPSHOT_MAX_TOTAL_BYTES:
            raise RuntimeError(
                f"克隆后工作区 {_fmt_bytes(ws_bytes)} 超过预算 "
                f"{_fmt_bytes(config.SNAPSHOT_MAX_TOTAL_BYTES)}："
                f"请把大目录/大文件加入项目 .gitignore（快照会尊重它），"
                f"或调大 config.SNAPSHOT_MAX_TOTAL_BYTES 后重试")
        msg(state["session_id"], "assistant",
            f"[clone] {how} → {ws}（{_fmt_bytes(ws_bytes)}）")
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
        # 分级（2026-11-01 用户拍板；白包阶段1/2026-11-06 升级）：python/javascript=容器执行
        # （Tier1/Tier2）；typescript 升 Tier2（镜像已备 ts-jest，散装走内联 transform 配置，
        # 见 _inline_jest_config）；java 移出"假 full"（镜像已备但生成/执行链路未接，白包阶段2）
        # → 走静态扫描兜底（Tier3）：扫描+测试设计草稿落库，报告明示"未执行"
        strategy = _tier_strategy(primary)
        # upsert：A4 允许重跑 done/failed 任务，重跑时模块行必须幂等（否则 UNIQUE 崩）
        cur = conn.execute(
            """INSERT INTO modules(project_id, path, language, loc, strategy) VALUES (?, '.', ?, ?, ?)
               ON CONFLICT(project_id, path) DO UPDATE SET language=excluded.language,
                 loc=excluded.loc, strategy=excluded.strategy""",
            (pid, primary, total, strategy))
        # 准出标准冻结进任务行（A1）：此后 decide 只认这个值，实验中途改 config 不影响在途任务
        conn.execute("UPDATE test_tasks SET coverage_target=? WHERE id=?",
                     (config.COVERAGE_TARGET, tid))
        # W4 热点快照 delete-first（与 plan/test_conditions 同款重跑幂等语义）：
        # 无条件清旧——重跑时主语言判定可能变化，残留行会让"热点未覆盖"指标失真
        conn.execute("DELETE FROM complexity_hotspots WHERE task_id=?", (tid,))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[analyze] 语言占比 {ratios} → 主语言 {primary}，分级 {strategy}，"
            f"准出标准冻结：行覆盖率 ≥ {config.COVERAGE_TARGET:.0%} 或预算耗尽")
        out = {"primary_language": primary, "module_id": cur.lastrowid,
               "coverage_target": config.COVERAGE_TARGET,
               "image": config.EXEC_LANGUAGES.get(primary, config.IMAGE_PY)}
        # W4 圈复杂度热点（radon 宿主侧确定性扫描）：注入 plan 提示词做风险分级倾斜；
        # 落库 complexity_hotspots（报告"热点未覆盖"指标的数据源，第 17 张表）
        if primary == "python":
            from ..skills.complexity import complexity_hotspots
            hotspots = complexity_hotspots(ws)
            if hotspots:
                out["complexity_hotspots"] = hotspots
                for h in hotspots:
                    conn.execute(
                        """INSERT INTO complexity_hotspots(task_id, file, name,
                                                          complexity, rank, lineno)
                           VALUES (?,?,?,?,?,?)""",
                        (tid, h["file"], h["name"], h["complexity"], h["rank"], h["lineno"]))
                conn.commit()
                msg(state["session_id"], "assistant",
                    "[analyze] W4 复杂度热点：" +
                    "; ".join(f"{h['file']}::{h['name']}={h['complexity']}({h['rank']})"
                              for h in hotspots)[:300])
        if strategy != "full":
            msg(state["session_id"], "assistant",
                f"[analyze] 主语言 {primary} 无容器执行链路 → 静态模式（Tier3 兜底）：确定性扫描 + "
                f"AI 测试设计草稿（不执行、无覆盖率），报告将明示未验证")
            return {**out, "static_only": True}   # 不再 done 短路：走 plan→static_draft→report
        return out

    # ---------- 3. 环境供给（容器 + 快照 + 依赖安装，全部留台账） ----------
    def provision(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "provisioning")
        # 环境 recipe（任务F）：确定性探测→白名单配方→构建+自检。成功=用更贴合项目
        # 版本的镜像；失败=回退预置镜像（老路径不变，不引入新失败面）。开关默认关。
        image = state["image"]
        if config.RECIPE_ENABLED:
            image = _try_recipe_image(state, tid, runner, msg) or image
        # 沙箱自适应（设计文档 §6）：镜像按需供给——任务级判定，缺镜像按模式分流
        auto = config.AUTO_PREPARE_ENV or bool(state.get("auto_prepare"))
        ready, how = prepare_task_image(runner, image, auto)
        if not ready:
            reason = ("执行镜像未就绪且未开启自动准备（模式①手动感知）："
                      "请在界面点「一键准备」或到设置页构建镜像，"
                      "或新建任务时勾选「自动准备环境」后重跑")
            msg(state["session_id"], "assistant", f"[provision] {reason}")
            return {"done": True, "verdict": "stop", "stop_reason": reason}
        msg(state["session_id"], "assistant", f"[provision] 执行镜像 {image} 就绪（{how}）")
        # 两阶段"布考场"（docs/两阶段人机协作流程设计-20261031.md）：快照前按审查结论
        # 重写生成用例文件（评审页勾掉/增补的用例以库为准），再补跑阶段一顺延的收集闸
        if state.get("two_stage_rebuild"):
            from ..skills.gen_tests import collect_gate_files, rebuild_gen_files_from_db
            n_files = rebuild_gen_files_from_db(conn, tid, Path(state["workspace"]))
            if n_files:
                msg(state["session_id"], "assistant",
                    f"[provision] 布考场：按审查结论重写 {n_files} 个生成用例文件（停用用例已摘除）")
        # 上一轮执行超时（exit=-1）说明容器里有 exec 残留，必须重建（A2），否则新旧任务争抢 CPU
        fresh = state.get("last_exit") == -1 and state.get("round_no", 0) > 0
        container = runner.ensure_container(image, state["container_name"], fresh=fresh)
        # 任务B：快照摘要落 agent_messages——跳过了什么/打了多大，界面可查
        # （#281 教训：7.7GB 工作区静默 OOM，用户只看到 MemoryError 不知道为什么）
        # 缺口②修复（2026-11-05）：差分闸开启时 .git 必须随行进考场，否则
        # run_diff_gate 在容器内恒 no_git（真机验证实锤）；默认关=语义不变
        snap = runner.copy_dir_into(container, state["workspace"],
                                    keep_git=config.DIFF_GATE_ENABLED)
        from ..sandbox.runner import _fmt_bytes
        snap_bits = [f"[provision] 工作区快照：{snap['file_count']} 个文件 "
                     f"{_fmt_bytes(snap['total_bytes'])} 进考场"
                     + (f"；尊重 .gitignore 排除 {len(snap['excluded_dirs'])} 个目录"
                        + (f"（{ '、'.join(d for d, _ in snap['excluded_dirs'][:5]) }…）"
                           if len(snap['excluded_dirs']) > 5 else
                           f"（{'、'.join(d for d, _ in snap['excluded_dirs'])}）")
                        if snap['excluded_dirs'] else "")
                     + (f"；跳过超限单文件 {len(snap['skipped_files'])} 个"
                        if snap['skipped_files'] else "")]
        msg(state["session_id"], "assistant", "".join(snap_bits))
        if state.get("two_stage_rebuild"):
            from ..skills.gen_tests import collect_gate_files
            dropped = collect_gate_files(runner, container, conn, tid, Path(state["workspace"]))
            if dropped:
                msg(state["session_id"], "assistant",
                    "[provision] 阶段一顺延的收集闸：文件级废案 " + "、".join(dropped))
        dep = registry.invoke(tid, skills["install_deps"], {
            "container_name": state["container_name"],
            "deps_manifest": probe_deps_manifest(state["workspace"])})
        # DB 感知测试环境 v1（用户需求：带库程序测试时库要参与）：检测 DB 依赖 →
        # sqlite 家族自动建库/迁移/种子；服务端库如实降级提示。fail-open 不炸 provision。
        try:
            from ..skills.db_probe import detect_db_usage, prepare_database
            msg(state["session_id"], "assistant", f"[provision·db] 探针执行（工作区 {state['workspace']}）")
            det = detect_db_usage(Path(state["workspace"]))
            if det["family"]:
                msg(state["session_id"], "assistant",
                    f"[provision·db] 检测到数据库依赖：{det['family']}"
                    f"（证据 {len(det['evidence'])} 条，"
                    f"全家族 {det['all_families']}，备库脚本 {det['scripts'] or '无'}，"
                    f"SQL 文件 {det['sql_files'] or '无'}）")
                prep = prepare_database(runner, container, det, Path(state["workspace"]),
                                        state.get("primary_language", ""))
                msg(state["session_id"], "assistant",
                    f"[provision·db] {prep.get('status')}：{prep.get('note', '')[:200]}")
        except Exception as e:  # noqa: BLE001 —— 备库失败如实留提示，不炸主链路
            log.warning("DB 感知预备失败（不影响主链路）: %s: %s", type(e).__name__, e)
        cur = conn.execute(
            """INSERT INTO container_jobs(task_id, module_id, image, status, cpu_limit, mem_limit_mb,
                                          priority, timeout_at, container_id, started_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?, datetime('now', '+' || ? || ' seconds'), ?, datetime('now'))""",
            (tid, state.get("module_id"), image,
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
                    llm_fn=make_deepseek_fn(os.environ.get("DEEPSEEK_API_KEY"),
                                            task_id=tid),
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
        # B2+（419 教训）：需求-能力错配必须对用户显性可见——独立消息行（不只埋在
        # JSON 截断里），任务详情页/工作台实时日志直接可读
        try:
            _gsum = json.loads(res.output)
            _mm = _gsum.get("capability_mismatch")
            if _mm:
                msg(state["session_id"], "assistant",
                    f"[generate·需求-能力对齐警告] {_mm} 本次生成可能未覆盖你真正想测的功能，"
                    f"请核对需求与产品实际能力。")
        except (ValueError, AttributeError):
            pass
        msg(state["session_id"], "assistant", f"[generate 第{next_round}轮] {res.output[:400]}")
        return {"round_no": next_round}

    # ---------- 5. 执行（容器内 pytest，确认门已在注册表层把住） ----------
    def run(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "running")
        lang = state["primary_language"]
        if lang not in ("python", "javascript", "typescript", "java"):
            # 其余语言：诚实终止，不进归因循环——"链路未实现"不该被伪装成环境失败（A7）
            return {"done": True, "verdict": "stop",
                    "stop_reason": f"{lang} 执行链路 v1 未接入（诚实降级：static_only 路径走静态扫描），任务终止"}
        ws = Path(state["workspace"])
        # 环境闸（归因三闸之一，11.4 恢复计划 §3）：真实用例前金丝雀自检，失败 fail-fast——
        # task_366 教训：环境坏 12 条全错时系统照常产出"疑似产品缺陷"报告。默认关（V 变量惯例）。
        if config.ENV_GATE_ENABLED and state.get("round_no", 1) <= 1:
            from ..skills import attribution
            cres = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                  attribution.canary_cmd(lang), timeout_s=120)
            if not attribution.canary_ok(cres.exit_code, cres.stdout):
                msg(state["session_id"], "assistant",
                    f"[env-gate] 金丝雀未通过（exit={cres.exit_code}）：任务判环境无效，"
                    f"不产出任何产品缺陷结论（fail-fast）")
                return {"done": True, "verdict": "stop",
                        "stop_reason": "环境闸：金丝雀自检未通过（框架/IO），判环境无效——"
                                       "不产出产品缺陷结论（11.4 归因三闸）"}
            msg(state["session_id"], "assistant", "[env-gate] 金丝雀通过：框架/IO 自检 OK")
        if lang in ("javascript", "typescript"):
            # JS/TS 执行链路（Tier2）：jest --json 输出经宿主侧适配器转 pytest 风格文本，
            # 下游 evaluate/repair/report 零改动（docs：js_adapter 模块头）。
            # TS 同路径（白包阶段1）：.test.ts 生成物 + 散装时内联 ts-jest transform 配置
            gen_ext = ".test.ts" if lang == "typescript" else ".test.js"
            gen_files = sorted(p.name for p in ws.glob(f"test_gen_*{gen_ext}"))
            imported = f"test_imported{gen_ext}"
            if (ws / imported).exists():
                gen_files.append(imported)                 # 人工/插入用例（按语言命名）
            cmd = build_run_cmd_js(round_no=state.get("round_no", 1), gen_files=gen_files)
            # 散装前端 JS/TS（853刷题系统实锤：无 package.json/jest.config → 全局 jest 报
            # "Could not find a config file" 拒跑 → 0/0 循环熔断）：无任何 jest 配置时
            # 注入最小内联配置（不动项目文件；有配置的项目走自己的）。
            # TS 加严（白包阶段1）：jest 默认配置无 transform，.test.ts 一律跑不动——
            # 只要有 package.json 但未声明 jest 配置（无 jest.config.* 且 package.json
            # 无 "jest" 键）也注入含 ts-jest transform 的内联配置（js-calc 实锤：带
            # package.json 的裸 jest 对 .js 可跑，对 .ts 不可）。
            has_jest_cfg = any((ws / n).exists() for n in
                               ("jest.config.js", "jest.config.ts", "jest.config.mjs",
                                "jest.config.cjs", "jest.config.json")) \
                or _pkg_declares_jest(ws)
            needs_inline = (not has_jest_cfg) and (
                lang == "typescript" or not (ws / "package.json").exists())
            if needs_inline:
                cmd += ["--config", _inline_jest_config(include_ts=(lang == "typescript"))]
                msg(state["session_id"], "assistant",
                    f"[run] 项目无 jest 配置{'/package.json' if not (ws / 'package.json').exists() else ''}："
                    f"注入最小内联配置执行（{'TypeScript（含 ts-jest transform）' if lang == 'typescript' else '散装 JS'} 兜底）")
        elif lang == "java":
            # Java 执行链路（Tier2，白包阶段2）：mvn test → surefire XML/JaCoCo 经 java_adapter
            # 转 pytest 风格文本，下游 evaluate/repair/report 零改动（adapter 统一文本唯一接缝）。
            cmd = build_run_cmd_java()
        else:
            # Loop-9：round_no>1 起 cmd 限定生成物+imported——自带 tests 恒定失败不进 R-Loop
            # （三角色实验 30 题实锤：混入后补测永远修不好→熔断/预算耗尽）；
            # 两阶段对话增补文件（test_gen_fb*.py）同属生成物，一并纳入
            gen_files = sorted(p.name for p in ws.glob("test_gen_r*.py"))
            gen_files += sorted(p.name for p in ws.glob("test_gen_fb*.py"))
            if (ws / "test_imported.py").exists():
                gen_files.append("test_imported.py")
            cmd = build_run_cmd(round_no=state.get("round_no", 1), gen_files=gen_files)
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
        # 任务C：逐用例结果落库（轮次级粒度，每轮跑完即出现——看板数据源）
        try:
            from ..skills.case_results import parse_jest_results, parse_junit_xml, store_case_results
            if lang in ("javascript", "typescript"):
                rows = parse_jest_results(r.get("stdout", ""))   # 转换前抽原始 jest JSON
            elif lang == "java":
                # surefire XML 目录逐文件 cat（容器内无 glob 展开——sh -c 罗列再逐个取），
                # parse_junit_xml 直接复用（surefire 与 pytest junitxml 同 family，S1 调研结论）
                ls = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                    ["sh", "-c",
                                     "ls target/surefire-reports/TEST-*.xml 2>/dev/null"],
                                    timeout_s=15, workdir="/work")
                rows = []
                for xf in ls.stdout.split():
                    jc = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                        ["cat", xf], timeout_s=15, workdir="/work")
                    if jc.ok:
                        rows.extend(parse_junit_xml(jc.stdout))
            else:
                jc = runner.exec_in(
                    runner.client.containers.get(state["container_name"]),
                    ["cat", config.JUNIT_PATH], timeout_s=30)
                rows = parse_junit_xml(jc.stdout) if jc.ok else []
            n_stored = store_case_results(conn, tid, state.get("round_no", 1), rows) if rows else 0
            if n_stored:
                msg(state["session_id"], "assistant",
                    f"[run 第{state.get('round_no', 1)}轮] 逐用例结果 {n_stored} 条落库"
                    f"（通过 {sum(1 for x in rows if x['status'] == 'passed')}"
                    f" / 失败 {sum(1 for x in rows if x['status'] == 'failed')}）")
        except Exception as e:              # noqa: BLE001 —— 看板数据缺失不炸执行主链路
            log.warning("逐用例结果落库失败 task=%s: %s", tid, e)
        if lang in ("javascript", "typescript") and r.get("stdout"):
            from ..skills.js_adapter import jest_json_to_pytest_text
            r["stdout"] = jest_json_to_pytest_text(r["stdout"], r.get("stderr", ""))
        if lang == "java":
            # java_adapter：surefire XML+JaCoCo → 统一文本（S5 唯一合法接缝，下游零改动）
            from ..skills.java_adapter import surefire_dir_to_pytest_text
            rep = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                 ["sh", "-c", "mkdir -p /tmp/sf && "
                                  "cp target/surefire-reports/TEST-*.xml /tmp/sf/ 2>/dev/null; "
                                  "ls /tmp/sf/ >/dev/null 2>&1 && echo ok"],
                                 timeout_s=20, workdir="/work")
            if rep.ok and "ok" in rep.stdout:
                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    for xf in runner.exec_in(runner.client.containers.get(state["container_name"]),
                                             ["sh", "-c", "ls /tmp/sf/"], timeout_s=10,
                                             workdir="/").stdout.split():
                        xj = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                            ["cat", f"/tmp/sf/{xf}"], timeout_s=15, workdir="/")
                        if xj.ok:
                            (Path(td) / xf).write_text(xj.stdout, encoding="utf-8")
                    r["stdout"] = surefire_dir_to_pytest_text(td)
            else:
                r["stdout"] = (r.get("stdout") or "") + "\n(no surefire reports: 编译失败或用例未执行)\n"
        return {"last_exit": r["exit"], "last_stdout": r["stdout"], "last_stderr": r["stderr"],
                "last_cmd": cmd}

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
        # —— 归因三闸·差分闸（11.4 恢复计划 §3；LIBRO buggy/fixed 口径，默认关）——
        # 疑似产品缺陷先在对照 ref 复跑失败子集：对照也失败 → 排除产品缺陷（用例/环境错），
        # 对照通过 → 高置信缺陷暴露。不可用（no_git/no_ref）→ 维持原归因，不硬猜。
        if (config.DIFF_GATE_ENABLED and kind == "product_defect"
                and state.get("last_exit", 1) != 0):
            from ..skills import attribution
            lang = state.get("primary_language", "python")
            failed_files = sorted({nid.split("::")[0]
                                   for nid in repair.failing_nodeids(stdout)})
            if failed_files:
                def _exec(cmd, timeout_s=600):
                    return runner.exec_in(
                        runner.client.containers.get(state["container_name"]),
                        cmd, timeout_s=timeout_s)
                diff = attribution.run_diff_gate(
                    _exec, lang, failed_files,
                    state.get("diff_ref") or config.DIFF_GATE_REF,
                    bug_stderr=state.get("last_stderr", ""))
                if diff["label"] == "case_defect":
                    kind = "case_defect"
                    # detail 自带「对照 <ref> 上…」全句，这里不再重复拼接（真机验证实锤）
                    reason = (f"差分闸降级（{diff['confidence']}置信）："
                              f"{diff['detail']} → 非产品缺陷（用例/环境错）")
                msg(state["session_id"], "assistant",
                    f"[diff-gate] status={diff['status']} {diff['detail']}"
                    f"{('；原归因 product_defect 已降级为 ' + kind) if kind != 'product_defect' else ''}"
                    if diff["status"] == "ok" else
                    f"[diff-gate] 不可用（{diff['status']}）：{diff['detail']}，维持原归因")
                if diff["status"] == "restore_failed":
                    msg(state["session_id"], "assistant",
                        "[diff-gate] ⚠ 工作区未切回原 HEAD——必须人工检查 git 状态后再用该工作区")
        # —— 归因三闸·flaky 连跑（TestGen-LLM 口径，默认 FLAKY_RERUNS=0 关）——
        # 疑似产品缺陷终判前同命令连跑 N 次：结果不一致 → stop_reason 标 flaky，
        # 报告侧据此把该轮不计入缺陷分母（taxonomy 不新增值，v1 靠 reason 携带）。
        if (config.FLAKY_RERUNS > 0 and kind == "product_defect"
                and state.get("last_exit", 1) != 0 and state.get("last_cmd")):
            from ..skills import attribution
            lang = state.get("primary_language", "python")
            outcomes = []
            for _ in range(int(config.FLAKY_RERUNS)):
                rr = runner.exec_in(
                    runner.client.containers.get(state["container_name"]),
                    state["last_cmd"], timeout_s=config.DEFAULT_JOB_TIMEOUT_S)
                outcomes.append(attribution.outcome_passed(
                    lang, rr.exit_code, rr.stdout))
            if outcomes and attribution.is_flaky(outcomes):
                reason += (f"；flaky 连跑 {config.FLAKY_RERUNS} 次结果不一致"
                           f"（{sum(outcomes)} 过/{len(outcomes) - sum(outcomes)} 败）"
                           "→ 证据强度降级，建议人工复跑定性")
        round_no = state.get("round_no", 0)
        # P1-③：每轮评估结构化落库（evaluation_rounds，轨迹唯一真源）——此前只写
        # 叙述消息，collect_metrics 靠正则解析散文取轨迹，措辞一变即静默清空。
        # 重跑再进图 round 从 1 重新计：delete-first 以本轮为准（与 plan 同幂等语义）
        if round_no <= 1:
            conn.execute("DELETE FROM evaluation_rounds WHERE task_id=? AND level='L1'",
                         (tid,))   # 级别过滤：L1 重跑不得清掉 L2 leg 的轮次行
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
        # 0/0 轮根因留痕（853刷题系统实锤：jest 输出解析不到时 0/0/N/A，用户只看到
        # "环境类失败"却不知道 jest 到底报了什么）——把原始输出尾部写进日志
        if passed == 0 and failed == 0:
            tail = (stdout or stderr or "")[-400:].strip()
            msg(state["session_id"], "assistant",
                "[evaluate·raw] 0 用例 0 失败（解析不到测试计数），执行输出尾部：" + (tail or "（空输出）"))
        # per-case 失败明细进日志（/defects API 与归因面板的数据源——此前从未写入，
        # F18-c 的 FAILED 行提取一直为空；Python/JS 同口径：nodeid - 错误类: 失败消息摘要）
        for nid in repair.failing_nodeids(stdout)[:20]:
            msg(state["session_id"], "assistant",
                f"[evaluate·failed] FAILED {nid} - "
                f"{repair.failure_summary_of(stdout, nid) or repair.error_class_of(stdout, nid) or 'Error'}")
        return {"passed": passed, "failed": failed, "skipped": skipped,
                "coverage_line": coverage, "per_file": _parse_per_file_coverage(stdout),
                "fingerprint": fp, "failure_kind": kind,
                "verdict": verdict, "stop_reason": reason,
                "witness_nodeids": witness,
                "last_stdout": stdout,   # 408 实锤：验证轮 env_blocked 分类需要诊断段，不回传即证据蒸发
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
        from ..skills.llm_client import make_chat  # B5：带退避重试的同签名构造
        llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=api_key,
                         base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                         timeout=60, max_retries=2)
        # BREADTH 广度采样（默认关=source_modules=None，提示词逐字节不变）：
        # plan 注入被测源码结构清单+广度纪律——治「条件全挤核心函数域」（E9 task432 实锤）
        src_outline = None
        if config.BREADTH_SAMPLING_ENABLED:
            from ..skills.gen_tests import source_outline
            ws = state.get("workspace") or (config.DATA_DIR / "workspaces" / f"task_{tid}")
            src_outline = (source_outline(Path(ws),
                                          (state.get("module_hint") or "python").lower())
                           or {}).get("files")
            if src_outline:
                msg(state["session_id"], "assistant",
                    f"[plan] 广度采样：源码结构清单 {len(src_outline)} 项注入拆解提示词")
        resp = llm.invoke([("system", PLAN_SYSTEM),
                           ("user", build_plan_prompt(requirement,
                                                      hotspots=state.get("complexity_hotspots"),
                                                      source_modules=src_outline))])
        log_llm_call(tid, "plan", resp)
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

    # ---------- 2.6 静态模式兜底（Tier3，2026-11-01）：扫描 + AI 设计草稿，不执行 ----------
    def static_draft(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "generating")   # 复用"生成"语义：生成的是设计草稿非可执行用例
        ws = Path(state["workspace"])
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        summary: dict
        if api_key:
            from ..skills.static_scan import generate_static_drafts
            out = generate_static_drafts(api_key, tid, ws, state.get("requirement", ""), conn)
            summary = json.loads(out)
            msg(state["session_id"], "assistant",
                f"[static-draft] 扫描+设计草稿：{summary.get('drafts', 0)} 条草稿落库"
                f"（framework=static_draft），扫描 {summary.get('scan', {}).get('files', '?')} 文件/"
                f"{summary.get('scan', {}).get('risk_count', '?')} 风险线索"
                if summary.get("drafts") else f"[static-draft] {summary.get('reason', out[:200])}")
        else:
            registry.invoke(tid, skills["static_scan"], {"workspace": str(ws)})
            summary = {"drafts": 0}
            msg(state["session_id"], "assistant",
                "[static-draft] 无 API Key：仅完成确定性扫描（详见审计台账），草稿跳过（诚实降级）")
        conn.commit()
        return {"done": True, "verdict": "pass",
                "failure_kind": "static",
                "stop_reason": ("静态模式（Tier3）：确定性扫描+测试设计草稿已落库，未在容器执行——"
                                "无覆盖率/R-Loop 数据，报告明示未验证"),
                "static_summary": summary}

    # ---------- 8. 评估阶段：五工具编排 + 报告二次审查（vision §1.6 双闸回炉） ----------
    def review(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "reporting")   # 评估属报告阶段；状态机 CHECK 枚举不扩（老库无 CHECK 迁移）
        verdict = state.get("verdict", "")
        ok = verdict == "pass"
        try:
            result = _review_body(state, tid, verdict)
        finally:
            # 终态恢复：report 节点已写 done/failed，review 入口的 reporting 只是过程态；
            # 无论审查 pass/reject，任务终态跟随执行判定（审查结论落 test_reports.review，不改判任务）
            set_status(conn, tid, "done" if ok else "failed")
        # 缺陷交接自动落盘（用户 2026-11-04 硬要求：每个任务到最后一步必须生成）；
        # 失败只留痕不上抛——交付物生成问题不允许改判任务终态（诚实边界）
        session = state.get("session_id")
        try:
            auto_export_handoff(conn, tid,
                                notify=(lambda s: msg(session, "assistant", s)) if session else None)
        except Exception as e:
            log.warning("缺陷交接自动落盘失败（不影响任务终态）: %s", e)
        return result

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
            "review": review, "static_draft": static_draft}
