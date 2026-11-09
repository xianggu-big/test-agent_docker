"""内置测试 Skill：与 L4 沙箱联动，全部经 SkillRegistry 调用以留审计台账。

- install_deps：低风险，容器内装项目依赖（依赖清单随项目走、不进镜像：
  requirements.txt / pyproject.toml / setup.py 三类探测分流）
- run_tests：高风险，需人工确认后才能下发（fail-closed 确认门）
- gen_tests：调用 LLM 生成用例；未配置 API Key 时诚实降级（跳过生成）
"""
import json
import logging
import os
from pathlib import Path

from .. import config
from .registry import Skill

log = logging.getLogger(__name__)


def probe_deps_manifest(workspace: str | Path) -> str | None:
    """探测被测项目的依赖清单类型（provision 探测、install_deps 分流安装）。

    返回 "requirements" | "pyproject" | "setup_py" | "package_json" | "ts_loose" | None。
    requirements.txt 优先：显式清单最常见且安装最便宜；pyproject.toml / setup.py 项目走
    `pip install .`；package.json（JS/TS 执行链路 Tier2）走 npm ci/install --ignore-scripts；
    ts_loose（白包阶段1）=无任何清单但有 .ts 源——不装项目依赖，只补 ts-jest 工具链
    （全局 ts-jest transformer 实测不可用，本地 node_modules 才有完整依赖树）。
    全部缺返回 None（诚实跳过，不猜依赖）。
    """
    ws = Path(workspace)
    for name, kind in (("requirements.txt", "requirements"),
                       ("pyproject.toml", "pyproject"),
                       ("setup.py", "setup_py"),
                       ("package.json", "package_json"),
                       ("composer.json", "composer_json")):
        if (ws / name).exists():
            return kind
    if _has_ts_source(ws):
        return "ts_loose"
    return None


def _has_ts_source(ws: Path, max_depth: int = 3) -> bool:
    """散装 TS 判定：深度≤3 扫描 .ts 源（排除 node_modules 等噪声目录）。
    与容器内 find -maxdepth 3 -name '*.ts' 同口径（_ts_tooling_needed 用）。"""
    import os
    root_depth = len(ws.parts)
    for dirpath, dirnames, filenames in os.walk(ws):
        if len(Path(dirpath).parts) - root_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in config.SKIP_DIRS]
        if any(f.endswith(".ts") for f in filenames):
            return True
    return False


def _ts_tooling_needed(runner, container) -> bool:
    """TS 工具补装判定（白包阶段1）：项目有 .ts 源且本地 node_modules 缺 ts-jest。

    触发即由 install_deps 以 npm install --no-save 补装 ts-jest/typescript/@types
    （--no-save 不改被测项目 package.json——零侵入，依赖只在容器内生效）。
    带清单与散装（无 package.json）两形态都走此判定：全局 ts-jest transformer 实测
    不可用（2026-11-07 容器探针：npm 全局布局把 jest 依赖嵌套在 jest 包内，
    ts-jest require('jest-util') 经 NODE_PATH 顶层解析不到 → Cannot find module），
    本地 node_modules 才有完整依赖树；npm install --no-save 在无 package.json
    目录实测 exit 0 可用（依赖只在容器内生效，零侵入）。"""
    has_ts = runner.exec_in(container, ["sh", "-c",
                                        "find /work -maxdepth 3 -name '*.ts' "
                                        "-not -path '*/node_modules/*' -print -quit"],
                           timeout_s=30)
    if not (has_ts.ok and has_ts.stdout.strip()):
        return False
    has_tsjest = runner.exec_in(container, ["sh", "-c",
                                            "test -d /work/node_modules/ts-jest"],
                                timeout_s=15)
    return not has_tsjest.ok


_TS_TOOLING_CMD = ["npm", "install", "--no-save", "--ignore-scripts", "--no-audit",
                   "--no-fund", "ts-jest@29", "typescript@5", "@types/jest@29",
                   "@types/node@20"]


def _install_ts_tooling(runner, container) -> str | None:
    """补装 TS 测试工具链（白包阶段1）；不需要时返回 None。版本对齐镜像全局件
    （jest29/ts5），失败 exit_code 如实上报交给执行轮归因，不吞错。"""
    if not _ts_tooling_needed(runner, container):
        return None
    r = runner.exec_in(container, _TS_TOOLING_CMD, timeout_s=300)
    return f"ts_tooling(no-save ts-jest@29+typescript@5) exit={r.exit_code}"


def _try_ts_tooling(parts: list, runner, container) -> None:
    """在 package_json / ts_loose 分支内触发 TS 补装（其余清单不探测——Python 项目
    走 pytest 链路无需 jest 工具链，且保住各清单分支的命令序列契约）。"""
    try:
        ts_part = _install_ts_tooling(runner, container)
        if ts_part:
            parts.append(ts_part)
    except Exception as e:      # noqa: BLE001 —— 补装失败如实上报，交给执行轮归因
        parts.append(f"ts_tooling 补装探测失败：{type(e).__name__}: {e}")


def build_skills(runner) -> dict[str, Skill]:
    """构造内置 Skill 集合。runner: sandbox.runner.SandboxRunner"""

    def install_deps(task_id: int, args: dict) -> str:
        container = runner.client.containers.get(args["container_name"])
        # 老调用方只传 has_requirements 布尔（=存在 requirements.txt），保持兼容
        manifest = args.get("deps_manifest") or (
            "requirements" if args.get("has_requirements") else None)
        parts = []
        if manifest == "requirements":
            r = runner.exec_in(container, ["pip", "install", "-r", "requirements.txt"],
                               timeout_s=300)
            parts.append(f"requirements exit={r.exit_code}\n{r.stdout[-1500:]}{r.stderr[-500:]}")
        elif manifest in ("pyproject", "setup_py"):
            # PEP 621 / setup.py 项目：装项目自身，把 [project].dependencies 拉进来；
            # 构建失败时 exit_code 如实上报（诚实降级，不吞错）
            r = runner.exec_in(container, ["pip", "install", "."], timeout_s=300)
            parts.append(f"{manifest} exit={r.exit_code}\n{r.stdout[-1500:]}{r.stderr[-500:]}")
        elif manifest == "package_json":
            # JS：有 lock 走 npm ci（可复现），否则 npm install；--ignore-scripts 防 postinstall
            # 执行仓库任意脚本（提示注入/供应链防线，与 P1-4a import 白名单同哲学）
            has_lock = runner.exec_in(container, ["test", "-f", "package-lock.json"],
                                      timeout_s=10).exit_code == 0
            npm_cmd = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"] if has_lock \
                else ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
            r = runner.exec_in(container, npm_cmd, timeout_s=600)
            parts.append(f"package_json({'ci' if has_lock else 'install'}) exit={r.exit_code}\n"
                         f"{r.stdout[-1200:]}{r.stderr[-600:]}")
            _try_ts_tooling(parts, runner, container)   # 白包阶段1：TS 项目补工具链
        elif manifest == "composer_json":
            # 白包阶段4（PHP）：composer install（phpunit 等 dev 依赖随项目走）；
            # --no-interaction 非 TTY 必需；依赖随项目走原则同 JS
            r = runner.exec_in(container, ["composer", "install", "--no-interaction",
                                           "--no-progress", "--no-plugins"],
                               timeout_s=600)
            parts.append(f"composer_json(install) exit={r.exit_code}\n"
                         f"{r.stdout[-1200:]}{r.stderr[-600:]}")
        elif manifest == "ts_loose":
            # 白包阶段1：散装 TS（无任何清单但有 .ts 源）——不装项目依赖（无清单可装），
            # 只补 ts-jest 工具链；npm install --no-save 在无 package.json 目录实测可用
            _try_ts_tooling(parts, runner, container)
        else:
            parts.append("未发现依赖清单（requirements.txt / pyproject.toml / setup.py / package.json），跳过项目依赖安装")
        # 方案B：属性测试用例依赖 hypothesis——随开关装进容器（宿主侧由实验环境自备）
        if config.PBT_ENABLED:
            h = runner.exec_in(container, ["pip", "install", "hypothesis"], timeout_s=300)
            parts.append(f"hypothesis exit={h.exit_code}")
        return "\n".join(parts)

    def run_tests(task_id: int, args: dict) -> str:
        container = runner.client.containers.get(args["container_name"])
        r = runner.exec_in(container, args["cmd"], timeout_s=config.DEFAULT_JOB_TIMEOUT_S)
        # stdout/stderr 分开返回，供宿主侧 Evaluator 归因判读（执行与判定分离）
        return json.dumps({"exit": r.exit_code, "stdout": r.stdout, "stderr": r.stderr},
                          ensure_ascii=False)

    def gen_tests(task_id: int, args: dict) -> str:
        # 2026-11-09 修复（688/689/690 实锤）：原实现直接读 os.environ["DEEPSEEK_API_KEY"]，
        # local 档（ollama，无需 key）被判"未配置"→两轮跳过生成→出题 0 条。统一走
        # llm_profile.effective_key()：显式传参 → 当前 profile 的 key_env（local 恒 ollama）。
        from .llm_profile import current, effective_key, missing_key_message
        api_key = effective_key()
        if not api_key:
            return json.dumps({"generated": 0, "skip_reason": "no_key",
                               "reason": "未配置 Key，跳过用例生成（诚实降级）："
                                         + missing_key_message(current())},
                              ensure_ascii=False)
        from .gen_tests import generate_tests  # 延迟导入，避免未用 Key 时的加载开销
        return generate_tests(api_key, args, task_id, runner)

    def log_analyze(task_id: int, args: dict) -> str:
        # 黑盒能力补齐阶段1：应用日志 LLM 摘要（证据=app_logs 确定性行，引用闸在模块内）
        from .llm_profile import current, effective_key, missing_key_message
        api_key = effective_key()   # 同上：local 档不应被判无 Key
        if not api_key:
            return json.dumps({"skipped": "日志分析不可用（诚实降级）："
                                          + missing_key_message(current())},
                              ensure_ascii=False)
        from ..store.db import get_conn     # skill func 在线程池执行：连接必须自建
        from . import log_analyze as _la
        conn = get_conn()
        try:
            out = _la.run_log_analyze(api_key, conn, task_id,
                                      round_no=int(args.get("round_no") or 1))
            return json.dumps(out, ensure_ascii=False)
        finally:
            conn.close()

    from .db_assert import db_assert as _db_assert_fn

    def db_assert(task_id: int, args: dict) -> str:
        return _db_assert_fn(api_key=None, args=args, task_id=task_id, runner=runner)

    from .ui_assert import ui_assert as _ui_assert_fn

    def ui_assert(task_id: int, args: dict) -> str:
        return _ui_assert_fn(api_key=None, args=args, task_id=task_id, runner=runner)

    from .grpc_assert import grpc_assert as _grpc_assert_fn

    def grpc_assert(task_id: int, args: dict) -> str:
        return _grpc_assert_fn(api_key=None, args=args, task_id=task_id, runner=runner)

    def ui_idempotency(task_id: int, args: dict) -> str:
        # 多测试路线编排 期 A2：UI 交互幂等（oracle=状态不变量，不依赖 LLM 判定）
        from .ui_idempotency import run_idempotency
        return run_idempotency(args.get("api_key") or "", args, task_id, runner)

    return {
        "install_deps": Skill(
            name="install_deps", description="容器内安装被测项目 Python 依赖（三类清单探测分流）",
            func=install_deps, risk_level="low", timeout_ms=300_000,
            arg_schema={"container_name": "str", "deps_manifest": "str"}),
        "run_tests": Skill(
            name="run_tests", description="容器内执行 pytest（需人工确认后下发）",
            func=run_tests, risk_level="high", timeout_ms=config.DEFAULT_JOB_TIMEOUT_S * 1000,
            requires_confirmation=True,
            arg_schema={"cmd": "list[str]"}),
        "gen_tests": Skill(
            name="gen_tests", description="按五种方法生成测试用例（等价类/边界值/判定表/覆盖/错误推测）",
            func=gen_tests, risk_level="medium", timeout_ms=300_000,   # 2026-11-10：local 模型（qwen3-4b）生成常 >120s（711/713 timeout 实锤），调到 300s
            arg_schema={"workspace": "str", "requirement": "str", "module_hint": "str"}),
        "log_analyze": Skill(
            name="log_analyze", description="应用运行时日志 LLM 摘要与异常解释（证据=app_logs 确定性错误/警告行，叙述必须引用真实行号）",
            func=log_analyze, risk_level="low", timeout_ms=150_000,
            arg_schema={"round_no": "int"}),
        "db_assert": Skill(
            name="db_assert", description="DB 验证：验证 SQL 经只读闸（sqlglot 白名单 SELECT/SHOW，任一违规整案拒绝）后在 DB sidecar 内执行并落 db_asserts 表",
            func=db_assert, risk_level="medium", timeout_ms=120_000,
            arg_schema={"sql": "str", "container_name": "str", "engine": "str",
                        "database": "str", "round_no": "int"}),
        "ui_assert": Skill(
            name="ui_assert", description="UI 黑盒验证：操作序列经双闸（动作 schema+selector 真值，虚构元素整案剔除留痕）后在 Playwright 容器内真实执行，逐步截图+trace.zip 落库，失败场景出缺陷报告单",
            func=ui_assert, risk_level="medium", timeout_ms=300_000,
            arg_schema={"scenarios": "str(JSON)", "snapshot": "str(JSON)",
                        "container_name": "str", "url": "str",
                        "round_no": "int", "evidence_dir": "str"}),
        "grpc_assert": Skill(
            name="grpc_assert", description="gRPC 验证：RPC 调用经 method 闸（service+method 必须命中 proto 清单，虚构整条剔除留痕）后经 grpcurl 容器内真实调用并落 grpc_asserts 表",
            func=grpc_assert, risk_level="medium", timeout_ms=120_000,
            arg_schema={"calls": "str(JSON)", "proto_text": "str", "proto_file": "str",
                        "container_name": "str", "host": "str", "port": "int",
                        "round_no": "int"}),
        "ui_idempotency": Skill(
            name="ui_idempotency", description="UI 交互幂等（期 A）：对带 id 交互元素执行单击/双击/三连击，oracle=状态不变量（重复触发不得改变计数），不依赖 LLM 判定；结果对齐 evaluate/attribution 契约",
            func=ui_idempotency, risk_level="medium", timeout_ms=420_000,
            arg_schema={"workspace": "str"}),
    }
