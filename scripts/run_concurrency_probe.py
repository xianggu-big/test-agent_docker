"""并发容量探针（任务卡-C，2026-11-10）：N∈{1,3,5} 真实任务并发下发，测"系统现状下的容量边界"。

用法：
  python scripts/run_concurrency_probe.py --n 1            # 场景A 单任务基线
  python scripts/run_concurrency_probe.py --n 3            # 三工具链项目同时下发
  python scripts/run_concurrency_probe.py --n 5            # 五任务（三工具链+两 python）
  python scripts/run_concurrency_probe.py --n 3 --profile deepseek   # 对照格（可选）

隔离纪律（不碰生产库）：config.DB_PATH 与 DATA_DIR 整体重定向到
data/reports/concurrency/tmpdata_N<n>/（tmp 库内 seed local/deepseek profile）；
LLM 档切换走既有 profile 机制；exec_queue 不改（直接线程并发下发=观察"现状架构"）。

因变量（每任务）：端到端墙钟/终态/错误/用例数/llm_calls（次数·token·phase 分布）；
系统级：宿主 CPU%/内存%（psutil 5s 采样）+ mta-run-* 容器峰值并发（docker ps 15s 采样）。
产物：data/reports/concurrency/c_N<n>_<profile>_<ts>.json + 同名 .md 摘要表。

诚实边界（预注册）：单机 Windows 宿主；demo<3k 行；n=1~3 每格；本地模型
~16 tok/s 拉长端到端——容器层与 LLM 层容量结论分两条线陈述，不合并成单一数字。
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

CONC_DIR = ROOT / "data" / "reports" / "concurrency"

# 每格的项目配方：repo 目录 + 需求文本（小项目无 REQUIREMENT.md 用一句话规格）
DEMO = ROOT / "data" / "demo"
REQ_PY_CALC = ("对 calc 模块做单元测试：验证 add 的正常相加与大数；验证 div 的正常"
               "除法；除数为零时应有明确错误而非崩溃。")
PROJECTS = {
    "场景A": {"path": DEMO / "场景A", "req": REQ_PY_CALC},
    "js-calc": {"path": DEMO / "js-calc", "req": None},          # 有 REQUIREMENT.md
    "go-ver-gate": {"path": DEMO / "go-ver-gate",
                    "req": "对 Go 模块 calc 的函数做单元测试：覆盖正常运算与整数边界"
                           "（0、负数、极大值），失败时断言应指向具体预期值。"},
    "llmdemo": {"path": DEMO / "llmdemo", "req": REQ_PY_CALC},
    "db-shop": {"path": DEMO / "db-shop", "req": None},
}
GRADIENT = {1: ["场景A"],
            3: ["场景A", "js-calc", "go-ver-gate"],
            5: ["场景A", "js-calc", "go-ver-gate", "llmdemo", "db-shop"]}


def _setup_isolation(n: int, profile: str) -> None:
    """生产库/工作区整体隔离：DB 与 DATA_DIR 重定向到本实验 tmp 目录。"""
    from test_agent import config
    from test_agent.store.db import get_conn, init_db
    from test_agent.skills import llm_profile
    tmp = CONC_DIR / f"tmpdata_N{n}_{profile}"
    tmp.mkdir(parents=True, exist_ok=True)
    dbp = tmp / "c.db"
    if dbp.exists():
        dbp.unlink()                      # 每格全新库（跑批可重入）
    config.DB_PATH = dbp
    config.DATA_DIR = tmp
    init_db(dbp)
    conn = get_conn(dbp)
    conn.execute("INSERT INTO users(username, password_hash) VALUES ('cprobe', 'x')")
    conn.commit()
    try:
        llm_profile.apply({"provider": profile}, conn)         # default=该档
        conn.commit()
        assert llm_profile.load_doc(conn)["default"] == profile
    finally:
        conn.close()


def _create_task(conn, name: str) -> int:
    spec = PROJECTS[name]
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES(?,?,?)",
                       (uid, str(spec["path"]), f"c-probe-{name}")).lastrowid
    req = spec["req"]
    if not req:
        req = (Path(spec["path"]) / "REQUIREMENT.md").read_text(encoding="utf-8")
    return conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed, confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, req, uid)).lastrowid


def _sample_loop(samples: list, stop: threading.Event) -> None:
    """系统级采样：psutil 5s；docker ps 容器数 15s（子进程较慢降频）。"""
    cpu0 = psutil.cpu_percent(interval=None)      # 首次调用基线
    i = 0
    while not stop.is_set():
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        row = {"t": round(time.monotonic(), 1), "cpu_percent": cpu,
               "mem_percent": mem}
        if i % 3 == 0:                            # 15s 一轮容器计数
            try:
                r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                   capture_output=True, text=True, timeout=20)
                row["mta_containers"] = sum(1 for ln in r.stdout.splitlines()
                                            if ln.startswith("mta-run-"))
            except Exception as e:                # noqa: BLE001 —— 采样失败如实留痕
                row["docker_err"] = f"{type(e).__name__}"
        samples.append(row)
        i += 1
        stop.wait(4)                              # 采样耗时约 1s + 4s ≈ 5s 周期


def _task_metrics(db_path: Path, tid: int, wall: float, error: str | None) -> dict:
    """从 tmp 库汇总单任务因变量。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        task = conn.execute("SELECT status, error FROM test_tasks WHERE id=?",
                            (tid,)).fetchone()
        # AI 生成用例数（test_cases 经 test_scripts 挂任务）；逐用例执行结果另计
        cases = conn.execute(
            "SELECT COUNT(*) c FROM test_cases tc JOIN test_scripts ts "
            "ON tc.script_id=ts.id WHERE ts.task_id=?", (tid,)).fetchone()["c"]
        last = conn.execute(
            "SELECT passed, failed, verdict, failure_kind, stop_reason "
            "FROM evaluation_rounds WHERE task_id=? ORDER BY round_no DESC LIMIT 1",
            (tid,)).fetchone()
        skills = conn.execute(
            "SELECT skill_id, COUNT(*) c, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok, "
            "COALESCE(SUM(duration_ms),0) ms FROM skill_invocations "
            "WHERE task_id=? GROUP BY skill_id", (tid,)).fetchall() \
            if _has_table(conn, "skill_invocations") else []
        calls = conn.execute(
            "SELECT phase, COUNT(*) c, COALESCE(SUM(total_tokens),0) tok "
            "FROM llm_calls WHERE task_id=? GROUP BY phase", (tid,)).fetchall()
        return {"task_id": tid, "wall_clock_s": round(wall, 1),
                "status": task["status"], "error": (error or task["error"]),
                "honest_done": bool(task["status"] == "done" or last),
                # failed+有 verdict=诚实终态（抓到缺陷/预算耗尽如实上报），≠系统故障
                "cases": cases,
                "last_round": (dict(last) if last else None),
                "skills": {r["skill_id"]: {"calls": r["c"], "ok": r["ok"],
                                           "duration_ms": r["ms"]} for r in skills},
                "llm_calls_total": sum(r["c"] for r in calls),
                "tokens_total": sum(r["tok"] for r in calls),
                "by_phase": {r["phase"]: {"calls": r["c"], "tokens": r["tok"]}
                             for r in calls}}
    finally:
        conn.close()


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _run_batch(n: int, profile: str) -> dict:
    from test_agent import config
    from test_agent.graph.levels import run_task_levels
    from test_agent.store.db import get_conn
    names = GRADIENT[n]
    conn = get_conn()
    try:
        tids = [_create_task(conn, nm) for nm in names]
        conn.commit()
    finally:
        conn.close()
    print(f"[c] N={n} profile={profile} 任务={list(zip(names, tids))}")
    samples: list = []
    stop = threading.Event()
    sampler = threading.Thread(target=_sample_loop, args=(samples, stop), daemon=True)
    t0 = time.monotonic()
    sampler.start()
    barrier = threading.Barrier(len(tids))
    results = {}

    def _one(tid: int, name: str):
        barrier.wait()                            # 同时下发
        t1 = time.monotonic()
        error = None
        try:
            run_task_levels(tid, ("L1",))
        except Exception as e:                    # noqa: BLE001 —— 失败留痕不中断他任务
            error = f"{type(e).__name__}: {e}"[:300]
        results[name] = _task_metrics(config.DB_PATH, tid,
                                      time.monotonic() - t1, error)

    with ThreadPoolExecutor(max_workers=len(tids)) as ex:
        futs = [ex.submit(_one, tid, nm) for tid, nm in zip(tids, names)]
        for f in futs:
            f.result()
    stop.set()
    sampler.join(timeout=10)
    batch_wall = round(time.monotonic() - t0, 1)
    peak_containers = max((s.get("mta_containers", 0) for s in samples), default=0)
    rep = {"n": n, "profile": profile, "projects": names, "task_ids": tids,
           "batch_wall_s": batch_wall, "peak_mta_containers": peak_containers,
           "host": {"cpu_count": psutil.cpu_count(),
                    "mem_total_gb": round(psutil.virtual_memory().total / 1e9, 1)},
           "tasks": results,
           "samples": samples}
    ok = [r for r in results.values() if r.get("honest_done")]
    rep["honest_done_count"] = len(ok)
    rep["wall_mean_s"] = round(sum(r["wall_clock_s"] for r in results.values())
                               / len(results), 1)
    return rep


def _write_md(rep: dict, out: Path) -> None:
    lines = [f"# 并发探针 N={rep['n']}（{rep['profile']} 档）", "",
             f"- 批墙钟 **{rep['batch_wall_s']}s**；单任务均值 {rep['wall_mean_s']}s；"
             f"honest_done {rep['honest_done_count']}/{len(rep['tasks'])}；容器峰值并发 "
             f"{rep['peak_mta_containers']}；宿主 {rep['host']['cpu_count']} 核/"
             f"{rep['host']['mem_total_gb']}GB", "",
             "| 项目 | task | 墙钟s | 终态 | 用例 | llm次数 | token | 错误 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, r in rep["tasks"].items():
        lines.append(f"| {name} | {r['task_id']} | {r['wall_clock_s']} | "
                     f"{r['status']} | {r['cases']} | {r['llm_calls_total']} | "
                     f"{r['tokens_total']} | {(r['error'] or '')[:60]} |")
    out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, choices=(1, 3, 5), required=True)
    ap.add_argument("--profile", choices=("local", "deepseek"), default="local")
    a = ap.parse_args()
    _setup_isolation(a.n, a.profile)
    rep = _run_batch(a.n, a.profile)
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = CONC_DIR / f"c_N{a.n}_{a.profile}_{ts}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_md(rep, out)
    print(json.dumps({k: rep[k] for k in ("n", "profile", "batch_wall_s",
                                          "honest_done_count", "wall_mean_s",
                                          "peak_mta_containers")},
                     ensure_ascii=False))
    print(f"已落档 {out.name} + .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
