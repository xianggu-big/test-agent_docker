"""E6 真实项目批量驱动（论文 6.8 回填数据源）：本地快照 → 主图 L1 全链路 → 逐轮留档。

用法：
  python scripts/run_e6_realprojects.py --dry-run            # 只打印跑次矩阵+预估成本，零 LLM
  python scripts/run_e6_realprojects.py --n 1                # 每项目 1 轮（成本校准用）
  python scripts/run_e6_realprojects.py --n 3                # 正式批量（放量前需预算确认）
  python scripts/run_e6_realprojects.py --projects titlecase # 只跑指定项目

产物：data/reports/e6_<project>_r<i>.json（每轮一份，含任务 id/终态/指标/token 实测）
口径（诚实边界）：
- 被测项目用 codeload 下载的本地快照（内容=GitHub 主分支，clone 节点按本地目录拷贝；
  GitHub 来源 URL 如实记录在 json 供溯源）；不走在线 clone（批量跑数的网络抖动不进实验变量）。
- 失败/blocked 任务如实留档不重试美化——假失败与真缺陷的区分靠人工复核栏（manual_review）。
- 单次成本（token/元）以 llm_calls 实测为准；线性外推仅供 dry-run 参考（标未验证）。

成本实测（2026-10-31）：titlecase 单轮 11,103 token（n=1 跑测，见 data/reports/h1_titlecase_*_159.json）；
线性外推 1 元 ≈ 30-45k token（DeepSeek chat 牌价，未逐项验证）——dry-run 预估仅供排序。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.builder import run_task  # noqa: E402
from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "reports"

# E6 项目清单：name → (GitHub 来源 URL, 本地快照目录, 需求文本)
# 需求文本与 2026-10-31 首跑（任务159/161）保持一致，保证可比性
PROJECTS = {
    "titlecase": (
        "https://github.com/ppannuto/python-titlecase.git",
        ROOT / "data" / "candidates" / "python-titlecase",
        "对 titlecase 库（英文标题大小写转换）做单元测试：覆盖普通句子转换、"
        "缩写词保护（iPad、PHP）、首词与末词大写、小词（a/of/the/and 等）不大写规则、"
        "标点与数字边界、空字符串与特殊字符输入。行覆盖率目标 70%。",
    ),
    "schema": (
        "https://github.com/keleshev/schema.git",
        ROOT / "data" / "candidates" / "schema",
        "对 schema 库（数据校验）做单元测试：Schema 基本校验（类型/必填/可选 Optional）、"
        "And/Or 组合条件、Regex/Use 转换、SchemaError 异常消息、空数据与嵌套结构边界。"
        "行覆盖率目标 70%。",
    ),
    # 项目#3（T005 选定，预检视野 32.9%——切片核心 Cache 类已入视野，与 schema 同形态；
    # 三项目构成 整收100%/切片34%/切片33% 形态梯度，预检口径见 scripts/precheck_sources.py）
    "cachetools": (
        "https://github.com/tkem/cachetools.git",
        ROOT / "data" / "candidates" / "cachetools",
        "对 cachetools 库（缓存数据结构）做单元测试：LRUCache/TTLCache/RRCache 的"
        "容量淘汰与过期语义、get/set/pop 行为、miss 与 maxsize 边界、"
        "cached 装饰器命中与失效、keys 函数。行覆盖率目标 70%。",
    ),
}


def _ensure_snapshot(name: str, url: str, dest: Path) -> bool:
    """本地快照缺失时下载（codeload 直连，复用 clone 回退的同款通道）。"""
    if dest.exists() and any(dest.rglob("*.py")):
        return True
    dest.mkdir(parents=True, exist_ok=True)
    from test_agent.graph.nodes import _codeload_fallback
    print(f"[e6] 下载快照 {name} ← {url}")
    return _codeload_fallback(url, dest) is not None


def _create_task(conn, name: str, url: str, snap: Path, requirement: str) -> int:
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(snap), f"e6-{name}")).lastrowid
    return conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid


def _collect(conn, tid: int, final: dict, t0: float, name: str, url: str, round_no: int) -> dict:
    rep = conn.execute("SELECT passed, failed, skipped, coverage_line, defects, created_at"
                       " FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    lc = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens),0) AS t"
                      " FROM llm_calls WHERE task_id=?", (tid,)).fetchone()
    return {
        "experiment": "E6", "project": name, "source_url": url, "round": round_no,
        "task_id": tid, "final": {k: final.get(k) for k in
                                  ("verdict", "stop_reason", "passed", "failed",
                                   "coverage_line", "round_no")} if final else None,
        "report": dict(rep) if rep else None,
        "llm": {"calls": lc["c"], "total_tokens": lc["t"]} if lc else None,
        "duration_s": round(time.time() - t0, 1),
        "manual_review": None,  # 协议要求：product_defect 时人工复核填 oracle 错/真缺陷
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    n = int(args[args.index("--n") + 1]) if "--n" in args else 1
    sel = args[args.index("--projects") + 1].split(",") if "--projects" in args else list(PROJECTS)

    # 快照就绪检查（dry-run 也做，提前暴露下载问题）
    ready = {}
    for name in sel:
        if name not in PROJECTS:
            print(f"[e6] 未知项目 {name}（可选：{list(PROJECTS)}）")
            return 2
        url, snap, req = PROJECTS[name]
        ready[name] = _ensure_snapshot(name, url, snap) if not dry else snap.exists()

    runs = [(name, i) for name in sel for i in range(1, n + 1)]
    print(f"[e6] 跑次矩阵：{len(runs)} 轮 = {sel} × n={n}")
    print(f"[e6] 快照就绪：{ready}")
    if dry:
        # 成本预估：titlecase 实测 11,103 token/轮；其余按 8-25k token/轮区间外推（未验证）
        est_tokens = len(runs) * 18000
        print(f"[e6] dry-run 预估：总 token ≈ {est_tokens}（区间 {len(runs)*8}k-{len(runs)*25}k，未验证）；"
              f"按 1 元≈35k token 折 ≈ {est_tokens/35000:.2f} 元——放量前以 --n 1 实测为准")
        print("[e6] dry-run 结束，未调 LLM、未建任务")
        return 0

    if not all(ready.values()):
        print("[e6] 快照缺失，中止（先修下载）")
        return 2
    conn = get_conn()
    out_rows = []
    for name, i in runs:
        url, snap, req = PROJECTS[name]
        tid = _create_task(conn, name, url, snap, req)
        conn.commit()
        print(f"[e6] {name} r{i} → 任务 #{tid} 开始 ...")
        t0 = time.time()
        try:
            final = run_task(tid)
        except Exception as e:  # 环境类崩溃也要留档（诚实边界）
            print(f"[e6] {name} r{i} 任务 #{tid} 异常：{e}")
            final = None
        row = _collect(conn, tid, final, t0, name, url, i)
        out = OUT / f"e6_{name}_r{i}.json"
        out.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        out_rows.append(row)
        print(f"[e6] {name} r{i} → {row['final']} | token {row['llm']} | {out.name}")
    ok = sum(1 for r in out_rows if r["final"] and r["final"].get("verdict") in ("pass", "stop"))
    print(f"[e6] 完成 {len(out_rows)} 轮（终态可判 {ok}）；结果清单："
          + ", ".join(f"{r['project']}#{r['task_id']}" for r in out_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
