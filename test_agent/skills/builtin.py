"""内置测试 Skill：与 L4 沙箱联动，全部经 SkillRegistry 调用以留审计台账。

- install_deps：低风险，容器内装项目依赖（requirements.txt 随项目走、不进镜像）
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


def build_skills(runner) -> dict[str, Skill]:
    """构造内置 Skill 集合。runner: sandbox.runner.SandboxRunner"""

    def install_deps(task_id: int, args: dict) -> str:
        container = runner.client.containers.get(args["container_name"])
        parts = []
        if args.get("has_requirements"):
            r = runner.exec_in(container, ["pip", "install", "-r", "requirements.txt"],
                               timeout_s=300)
            parts.append(f"requirements exit={r.exit_code}\n{r.stdout[-1500:]}{r.stderr[-500:]}")
        else:
            parts.append("无 requirements.txt，跳过项目依赖安装")
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
            name="install_deps", description="容器内安装被测项目 Python 依赖",
            func=install_deps, risk_level="low", timeout_ms=300_000,
            arg_schema={"container_name": "str", "has_requirements": "bool"}),
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
