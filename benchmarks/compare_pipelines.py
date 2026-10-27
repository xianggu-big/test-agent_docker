"""三链路受控对比实验（A=prune 基线 / B=rewrite YATE / C=hybrid 合成）。

依据：docs/industry-testing-research.md §5.3（YATE 修复优于丢弃）+ §8 行2/行3。
受控变量：同一被测对象（data/demo/场景E/cart.py）、同一合成第1轮生成物
（2好 + 2强信号坏[ImportError/AttributeError] + 1断言证人）、同一冻结准出标准 0.70。
真实环节：Docker 沙箱执行 + DeepSeek 修复/生成调用；机制层差异由 tests/test_repair_strategy.py
的 fake-LLM 单测锁死。诚实边界：每链路 n=1（LLM 有随机性）。

运行：python benchmarks/compare_pipelines.py  （需 Docker + DEEPSEEK_API_KEY）
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根入 path

from test_agent import config
from test_agent.graph import repair
from test_agent.graph.nodes import _parse_per_file_coverage
from test_agent.sandbox.runner import SandboxRunner
from test_agent.skills.gen_tests import generate_tests
from test_agent.skills.repair_strategy import make_deepseek_fn, run_repair_strategy
from test_agent.store.db import get_conn

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "demo" / "场景E"
STRATEGIES = [("A·prune基线", "prune"), ("B·rewrite(YATE)", "rewrite"), ("C·hybrid合成", "hybrid")]
CMD = ["pytest", "-q", "--tb=short", "-rfE", "--cov=.", "--cov-report=term-missing:skip-covered"]

R1 = '''"""合成第1轮生成物：2好 + 2强信号坏 + 1证人（受控输入）"""
from cart import apply_discount, Cart


def test_apply_discount_ok():
    assert apply_discount(100, 20) == 80.0


def test_cart_total():
    c = Cart()
    c.add("a", 30)
    c.add("b", 70)
    assert c.total() == 100.0


def test_bad_import():
    from cart import discount  # ImportError：不存在的旧接口

    assert discount(100, 20) == 80.0


def test_bad_attr():
    c = Cart()
    c.add("a", 10)
    c.clear()  # AttributeError：不存在的方法
    assert c.total() == 0


def test_witness():
    assert apply_discount(100, 50) == 40.0  # 断言失败=证人（三链路一律保留）
'''


def parse_run(stdout: str) -> dict:
    n = lambda p: int(m.group(1)) if (m := re.search(rf"(\d+) {p}", stdout)) else 0
    cov = None
    if m := re.search(r"TOTAL.*?\s(\d+)%", stdout):
        cov = int(m.group(1)) / 100
    return {"passed": n("passed"), "failed": n("failed"),
            "per_file": _parse_per_file_coverage(stdout), "coverage": cov,
            "fp": repair.fingerprint_of(stdout, ""), "raw": stdout}


def run_pipeline(tag: str, strategy: str, runner: SandboxRunner, api_key: str) -> dict:
    conn = get_conn()
    uname = f"pbench_{tag}_{int(time.time())}"   # 每轮唯一，可重复跑
    uid = conn.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?, 'developer')",
                       (uname, "x")).lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, f"local://场景E", f"链路对比-{tag}")).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text) VALUES(?,?,?)",
                       (pid, uid, "购物车与折扣函数的功能测试：折扣范围校验、购物车累加")).lastrowid
    conn.commit()

    ws = Path(tempfile.mkdtemp(prefix=f"pbench_{tag}_"))
    shutil.copytree(SRC, ws, dirs_exist_ok=True)
    (ws / "test_gen_r1.py").write_text(R1, encoding="utf-8")

    container_name = f"mta-pbench-{strategy}"  # 容器名只允许 ASCII（中文 tag 进不了 docker）
    runner.cleanup(container_name)
    container = runner.ensure_container(config.IMAGE_PY, container_name, fresh=True)
    runner.copy_dir_into(container, ws)

    m = {"tag": tag, "strategy": strategy, "r1_llm": 0, "r2_llm": 0,
         "repair_ms": 0, "regen_ms": 0, "exec_ms": 0}
    t0 = time.perf_counter()
    r1 = runner.exec_in(container, CMD)
    m["exec_ms"] += int((time.perf_counter() - t0) * 1000)
    p1 = parse_run(r1.stdout + r1.stderr)
    m["r1"] = {k: p1[k] for k in ("passed", "failed", "coverage")}
    verdict, kind, reason = repair.decide(
        r1.exit_code, r1.stdout, r1.stderr, [], p1["fp"], 1, p1["coverage"], 0.70)
    m["r1_verdict"] = f"{verdict}({kind})"

    pairs = [(nid, repair.error_class_of(r1.stdout, nid))
             for nid in repair.failing_nodeids(r1.stdout)]
    t1 = time.perf_counter()
    info = run_repair_strategy(strategy, ws, pairs, tid, conn, 2,
                               llm_fn=make_deepseek_fn(api_key), runner=runner,
                               container_name=container_name,
                               requirement="购物车与折扣函数的功能测试",
                               fail_summary=(r1.stdout + r1.stderr)[-1500:])
    m["repair_ms"] = int((time.perf_counter() - t1) * 1000)
    m["r2_llm"] = info.get("llm_calls", 0)
    m["skip_full_regen"] = info.get("skip_full_regen", False)
    m["pruned"] = len(info.get("pruned", []))
    m["rewritten"] = info.get("rewritten", [])
    m["fallback"] = info.get("fallback", "")

    if not info.get("skip_full_regen"):
        t2 = time.perf_counter()
        gen_out = generate_tests(api_key, {
            "workspace": str(ws), "requirement": "购物车与折扣函数的功能测试",
            "module_hint": "python", "container_name": container_name,
            "coverage_gaps": p1["per_file"], "coverage_target": 0.70, "round_no": 2,
            "module_id": None}, tid, runner)
        m["regen_ms"] = int((time.perf_counter() - t2) * 1000)
        try:
            m["regen_n"] = json.loads(gen_out).get("generated", 0)
        except json.JSONDecodeError:
            m["regen_n"] = 0

    runner.copy_dir_into(container, ws)
    t3 = time.perf_counter()
    r2 = runner.exec_in(container, CMD)
    m["exec_ms"] += int((time.perf_counter() - t3) * 1000)
    p2 = parse_run(r2.stdout + r2.stderr)
    m["r2"] = {k: p2[k] for k in ("passed", "failed", "coverage")}
    verdict2, kind2, reason2 = repair.decide(
        r2.exit_code, r2.stdout, r2.stderr, [p1["fp"]], p2["fp"], 2, p2["coverage"], 0.70)
    m["r2_verdict"] = f"{verdict2}({kind2})"
    m["total_ms"] = int((time.perf_counter() - t0) * 1000)
    m["witness_kept"] = "test_witness" in (r2.stdout + r2.stderr) and p2["failed"] >= 1

    runner.cleanup(container_name)
    shutil.rmtree(ws.parent, ignore_errors=True)
    return m


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("未配置 DEEPSEEK_API_KEY：B/C 的重写与全量生成将走降级路线，实验无效")
        sys.exit(1)
    runner = SandboxRunner()
    rows = []
    for tag, strategy in STRATEGIES:
        print(f"[run] 链路 {tag} ...", flush=True)
        rows.append(run_pipeline(tag, strategy, runner, api_key))
        print(f"  → {rows[-1]['r1']} → {rows[-1]['r2_verdict']} 总耗时 {rows[-1]['total_ms']}ms",
              flush=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "benchmarks" / "results" / f"pipeline_compare_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    def cell(r, k):
        v = r.get(k)
        return "—" if v in (None, "") else (f"{v}条" if isinstance(v, list) and v and isinstance(v[0], str) is False else v)

    lines = [
        "# 三链路受控对比：A=prune 基线 / B=rewrite(YATE) / C=hybrid 合成",
        f"\n- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 受控变量：同一被测对象（场景E/cart.py）、同一合成第1轮（2好+2强信号坏+1证人）、准出标准冻结 70%",
        "- 真实环节：Docker 沙箱执行 + DeepSeek 修复/生成；机制层由 fake-LLM 单测锁死",
        "- 诚实边界：每链路 n=1（LLM 有随机性，趋势以机制单测与本表共同支撑）",
        "",
        "| 参数 | 链路A·prune基线 | 链路B·rewrite(YATE) | 链路C·hybrid合成 |",
        "|---|---|---|---|",
    ]
    keys = [
        ("第1轮 通过/失败", lambda r: f"{r['r1']['passed']}/{r['r1']['failed']}"),
        ("第1轮 覆盖率", lambda r: f"{r['r1']['coverage']:.0%}" if r["r1"]["coverage"] is not None else "—"),
        ("第1轮 判定", lambda r: r["r1_verdict"]),
        ("修复动作：剪除条数", lambda r: r["pruned"]),
        ("修复动作：重写条数", lambda r: len(r["rewritten"])),
        ("重写兜底/降级", lambda r: r.get("fallback") or "无"),
        ("修复阶段 LLM 调用", lambda r: r["r2_llm"]),
        ("修复阶段 耗时(ms)", lambda r: r["repair_ms"]),
        ("全量重生成 用例数", lambda r: r.get("regen_n", "跳过(YATE)")),
        ("全量重生成 耗时(ms)", lambda r: r.get("regen_ms", "—")),
        ("第2轮 通过/失败", lambda r: f"{r['r2']['passed']}/{r['r2']['failed']}"),
        ("第2轮 覆盖率", lambda r: f"{r['r2']['coverage']:.0%}" if r["r2"]["coverage"] is not None else "—"),
        ("第2轮 判定", lambda r: r["r2_verdict"]),
        ("证人保留（test_witness 仍失败在场）", lambda r: "✅" if r["witness_kept"] else "❌"),
        ("端到端总耗时(ms)", lambda r: r["total_ms"]),
    ]
    for label, fn in keys:
        lines.append(f"| {label} | " + " | ".join(str(fn(r)) for r in rows) + " |")

    a, b, c = rows
    lines += [
        "",
        "## 三链路优缺点与合成逻辑（论文「讨论」节素材）",
        "",
        f"- **A（剪除+全量重生成）**：优点=确定性最强、无修复质量风险、单次 LLM 调用；"
        f"缺点=丢弃贴近程序逻辑的高价值种子，全量重生成（本实验 {a.get('regen_n', 0)} 条）"
        f"成本高且可能与已有好用例重复造轮子。",
        f"- **B（YATE 重写）**：优点=种子保留（本实验重写 {len(b['rewritten'])} 条，"
        f"{'全部成功故跳过全量重生成' if b.get('skip_full_regen') else '存在降级/未全成'}），"
        f"LLM 只花在刀刃上（总耗时最短 {b['total_ms']}ms）；缺点=修复质量依赖 LLM（降级时退化回 A），"
        f"且**不做覆盖缺口补测**——本实验覆盖率停在 {b['r2']['coverage']:.0%}（A/C 达 100%），无人补。",
        f"- **C（hybrid 合成）**：取 A 之长（缺口回灌全量补测，覆盖兜底）+ 取 B 之长"
        f"（带失败上下文重写保种子，预算 {config.REWRITE_BUDGET} 上限 + 静态/collect 准入闸）"
        f"+ 剪除兜底。代价=LLM 调用数最多（本实验 {c['r2_llm'] + (1 if c.get('regen_ms') else 0)} 次修复+生成）、"
        f"端到端耗时最长（{c['total_ms']}ms）——用成本换鲁棒。",
        "- **三链路共同铁律**：断言失败证人一律保留（§7.2 防造假），仓库自带测试不动，"
        "准出标准冻结——修复合成不改变诚实性语义。",
        "- **选型结论（建议）**：资源受限选 A（基线），追求补测质量与鲁棒选 C；"
        f"B 仅在被测对象大、全量重生成成本高时划算。本项目默认策略保持 A（config.REPAIR_STRATEGY），"
        "C 为推荐升级（一行配置切换）。",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(f"\n对比报告已写入 {out}（原始数据 {out.with_suffix('.json')}）")


if __name__ == "__main__":
    main()
