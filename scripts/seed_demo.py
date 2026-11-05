"""双项目种子自举对照演示：同一机制，两个不同项目环境 → 两种针对性模板。"""
import json, sys, shutil
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pathlib import Path
from test_agent import config
from test_agent.sandbox.runner import SandboxRunner
from test_agent.skills.gen_tests import _generate_seed, _collect_sources_detailed

runner = SandboxRunner()
DEMO = Path(r"D:\test agent_docker\data\demo\L2场景-coupon-api")
CORPUS = Path(r"D:\test agent_docker\data\corpus\corpus-001\project")

results = []
for tag, src_dir, req in (
    ("coupon-api", DEMO, "优惠券创建与下单（满 min_total 可用，折扣=amount*percent//100）"),
    ("checker 语料", CORPUS, "密码强度校验：长度 [8,64]、大小写数字特殊字符、强度评分与强密码判定"),
):
    ws = config.DATA_DIR / "workspaces" / f"seed_demo_{tag.replace(' ', '_')}"
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(src_dir, ws, ignore=shutil.ignore_patterns("__pycache__", ".git", "openapi.json", "*.md"))
    container = runner.ensure_container(config.IMAGE_PY, "mta-seed-demo", fresh=True)
    runner.copy_dir_into(container, str(ws))
    runner.exec_in(container, ["pip", "install", "-q", "pytest"], timeout_s=120)
    if (src_dir / "requirements.txt").exists():
        runner.exec_in(container, ["pip", "install", "-q", "-r", "requirements.txt"], timeout_s=300)
    import os
    from test_agent.skills.llm_client import make_chat
    llm = make_chat(model=config.DEEPSEEK_MODEL, api_key=os.environ.get("DEEPSEEK_API_KEY"),
                    base_url=config.DEEPSEEK_BASE_URL, temperature=0.2, timeout=90, max_retries=2)
    seed = _generate_seed(llm, ws, req, runner, container, "test_seed_probe.py", tries=3)
    srcs = _collect_sources_detailed(ws, "python")[0]
    results.append({"tag": tag, "module": srcs[0][0] if srcs else "?", "seed": seed})
    runner.cleanup("mta-seed-demo")

print("\n===== 双项目种子对照（同一机制，两个项目环境）=====")
for r in results:
    print(f"\n◆ 项目：{r['tag']}（被测模块 {r['module']}）")
    print("-" * 60)
    print(r["seed"] if r["seed"] else "（诚实降级：种子三轮未通过验证）")
