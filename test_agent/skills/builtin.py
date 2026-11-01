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

    返回 "requirements" | "pyproject" | "setup_py" | "package_json" | None。
    requirements.txt 优先：显式清单最常见且安装最便宜；pyproject.toml / setup.py 项目走
    `pip install .`；package.json（JS 执行链路 Tier2）走 npm ci/install --ignore-scripts。
    全部缺返回 None（诚实跳过，不猜依赖）。
    """
    ws = Path(workspace)
    for name, kind in (("requirements.txt", "requirements"),
                       ("pyproject.toml", "pyproject"),
                       ("setup.py", "setup_py"),
                       ("package.json", "package_json")):
        if (ws / name).exists():
            return kind
    return None


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
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            return "未配置 DEEPSEEK_API_KEY：跳过用例生成，运行仓库自带测试（诚实降级）"
        from .gen_tests import generate_tests  # 延迟导入，避免未用 Key 时的加载开销
        return generate_tests(api_key, args, task_id, runner)

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
            func=gen_tests, risk_level="medium", timeout_ms=120_000,
            arg_schema={"workspace": "str", "requirement": "str", "module_hint": "str"}),
    }
