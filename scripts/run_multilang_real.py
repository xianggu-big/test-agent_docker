"""多语言真实项目实验 driver（2026-11-08 实验轮，用户授权 run_task 全链路窗口）。

实验基建——只做编排与采集，不进产品代码（不改 test_agent/ 包内任何文件）：
- fetch：codeload tarball 拉取（git clone 历史核验不通）→ 解包核验（幂等）；
- gen-reqs：LLM 读核心源码出 5 条测试需求 → 固化 manifest（控制变量：n=3 全复用同一组）；
- run：逐需求建两阶段 auto 任务（queue 链路：prep→auto 代审→自动入队 exec→终态）
  → 采集用例数/通过率/轮次/token/归因 → 单任务结果 JSON（断点续跑：已有即跳过）；
- aggregate：聚合三件套（aggregate.md / stability.md / findings.md）。

隔离红线：产物只写 data/experiments/multilang/ 与 data/reports/multilang_real/；
任务识别=projects.repo_url 前缀 data/experiments/multilang/ + requirement_text 前缀
[multilang-real]；不触碰 E9/E10 产物与台账；不跑历史实验脚本。
停车线：连续 5 任务失败或失败率>30% → 停批；BudgetExhausted（402）→ 停批报告。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

BASE = ROOT / "data" / "experiments" / "multilang"
RESULTS = BASE / "results"
REPORT_DIR = ROOT / "data" / "reports" / "multilang_real"
LEDGER = ROOT / ".autonomous-loop" / "multilang-ledger.md"
TASK_TIMEOUT_S = 25 * 60          # 单任务终态轮询上限
READY_STALL_S = 12 * 60           # ready_for_review 停等超时（auto 钩子应自动接走）
REQUIREMENT_PREFIX = "[multilang-real] "
TERMINAL = {"done", "failed", "cancelled"}
NEWLINE = chr(10)


def _ledger_append(line: str) -> None:
    ts = time.strftime("%m-%d %H:%M")
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(f"- [{ts}] {line}\n")


def _load_manifest() -> dict:
    return json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))


def _save_manifest(m: dict) -> None:
    (BASE / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2),
                                        encoding="utf-8")


# —————————————————————————— 出题（LLM 读核心源码，固化 manifest） ————————————

_EXT_BY_LANG = {"ts": (".ts",), "java": (".java",), "go": (".go",), "php": (".php",)}
_NOISE_PAT = re.compile(r"(^|/|\\)(test|__tests__|tests|vendor|node_modules|"
                        r"\.git|target|build|dist|examples?|benchmark)(" + "|".join(["/|\\\\", "$", r"\."]) + ")", re.I)


def _core_sources(proj_dir: Path, lang: str, budget: int = 10000) -> str:
    """确定性挑核心源文件：按扩展名 rglob、排除测试/噪声、按大小排序取前 2 个，各截断。"""
    exts = _EXT_BY_LANG[lang]
    files = []
    for p in proj_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel = str(p.relative_to(proj_dir)).replace("\\", "/")
        if _NOISE_PAT.search(rel) or p.name.lower().endswith(("_test.go", ".test.ts", "_test.php")):
            continue
        try:
            files.append((p.stat().st_size, p, rel))
        except OSError:
            continue
    files.sort(reverse=True)
    parts, used = [], 0
    for size, p, rel in files[:2]:
        if size < 800:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        take = text[: max(2000, (budget - used) // 1)][:6000]
        parts.append(f"### {rel}\n```{lang}\n{take}\n```")
        used += len(take)
        if used >= budget:
            break
    return "\n\n".join(parts)


def gen_requirements(api_key: str, proj: dict) -> list[dict]:
    """LLM 读核心源码出 5 条测试需求；输出固化 manifest（n=3 复用同一组）。"""
    from test_agent.skills.llm_client import make_chat

    src = _core_sources(Path(proj["local_path"]), proj["lang"])
    if not src.strip():
        raise RuntimeError(f"{proj['project']} 无核心源文件可读")
    chat = make_chat(model=config.DEEPSEEK_MODEL, temperature=0,
                     api_key=api_key, base_url=config.DEEPSEEK_BASE_URL,
                     timeout=120, max_tokens=3000, phase="multilang_gen_reqs")
    sysmsg = (
        "你是测试需求出题人。根据给定开源项目核心源码，出恰好 5 条测试需求，"
        "覆盖不同函数/方法/接口（禁止 5 条全挤同一函数）。每条需求是交给自动化测试生成系统的"
        "自然语言需求文本：描述要测的功能点与明确预期（像 REQUIREMENT.md 的第 N 条），"
        "2-4 句话，可执行、可判定；不要写实现细节（被测系统自己读源码）。"
        '只输出 JSON：{"requirements":[{"title":"...","requirement":"..."}]}，不要任何解释。')
    resp = chat.invoke([
        ("system", sysmsg),
        ("user", f"项目：{proj['project']}（{proj['description']}）\n\n核心源码节选：\n{src}"),
    ])
    text = (getattr(resp, "content", "") or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    reqs = data.get("requirements")
    assert isinstance(reqs, list) and len(reqs) >= 5, f"出题数量不足: {len(reqs or [])}"
    return [{"title": r["title"][:80], "requirement": r["requirement"][:800]}
            for r in reqs[:5]]


# —————————————————————————— 跑批（建任务→队列→终态→采集） ————————————

def create_task(proj: dict, req: dict) -> int:
    """建两阶段 auto 实验任务（打标：repo 前缀 + requirement 前缀双保险）。"""
    from test_agent.store.db import get_conn
    from test_agent.store.projects import ensure_project
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
        uid = row["id"]
        pid, name, _ = ensure_project(conn, uid, str(Path(proj["local_path"]).resolve()))
        tid = conn.execute(
            """INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed,
                                      confirmed_by, two_stage, ai_reviewer)
               VALUES (?,?,?,1,?,1,'auto')""",
            (pid, uid, REQUIREMENT_PREFIX + f"[{proj['lang']}/{proj['project']}/{req['title']}] "
             + req["requirement"], "agent:multilang-driver")).lastrowid
        conn.commit()
        return tid
    finally:
        conn.close()


def _collect(tid: int) -> dict:
    """采集单任务实验指标（真实库读，不改任何历史数据）。"""
    from test_agent.store.db import get_conn
    conn = get_conn()
    try:
        t = dict(conn.execute("SELECT id, status, error FROM test_tasks WHERE id=?",
                              (tid,)).fetchone())
        cases = conn.execute(
            """SELECT COUNT(*) AS n FROM test_cases c JOIN test_scripts sc ON sc.id=c.script_id
               WHERE sc.task_id=? AND c.source='ai' AND c.enabled=1""", (tid,)).fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            "SELECT status FROM test_case_results WHERE task_id=?", (tid,))]
        passed = sum(1 for r in rows if r["status"] == "passed")
        failed = sum(1 for r in rows if r["status"] == "failed")
        errored = sum(1 for r in rows if r["status"] == "error")
        skipped = sum(1 for r in rows if r["status"] == "skipped")
        rounds = [dict(r) for r in conn.execute(
            """SELECT round_no, passed, failed, verdict, failure_kind, stop_reason
               FROM evaluation_rounds WHERE task_id=? AND level='L1' ORDER BY round_no""", (tid,))]
        tokens = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) AS t FROM llm_calls WHERE task_id=?",
            (tid,)).fetchone()["t"]
        llm_n = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE task_id=?", (tid,)).fetchone()["n"]
        rep = conn.execute(
            "SELECT coverage_line, defects FROM test_reports WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        defects = json.loads(rep["defects"]) if (rep and rep["defects"]) else {}
        # 归因分布：轮次 failure_kind 列 + 报告 defects.failure_kind + stop_reason 文本痕迹
        kinds = [r.get("failure_kind") for r in rounds if r.get("failure_kind")]
        stop = " ".join(str(r.get("stop_reason") or "") for r in rounds) + \
            " " + str(t.get("error") or "") + " " + str(defects.get("stop_reason") or "")
        attribution = {
            "failure_kinds": kinds,
            "reported_kind": defects.get("failure_kind"),
            "env_blocked_hit": "env_blocked" in stop or "env_blocked" in json.dumps(kinds),
            "flaky_hit": "flaky" in stop,
        }
        coverage = rep["coverage_line"] if rep else None
        return {"task_id": tid, "status": t["status"], "error": (t["error"] or "")[:300],
                "cases_generated": cases, "case_results": {"passed": passed, "failed": failed,
                                                           "error": errored, "skipped": skipped},
                "rounds": rounds, "tokens": tokens, "llm_calls": llm_n,
                "coverage_line": coverage, "attribution": attribution,
                "stop_reason": stop[:400]}
    finally:
        conn.close()


def result_path(cell: dict) -> Path:
    return RESULTS / f"{cell['lang']}_{cell['project']}_{cell['req_idx']}_{cell['n']}.json"


def run_cell(cell: dict, api_key: str, retry: int = 0) -> dict:
    """跑单格：建任务→入队→轮询终态→采集→写结果 JSON。失败重试至多 1 次（由调用方控制）。"""
    from test_agent.store.queue import QueueWorker, start_worker
    proj = cell["proj"]
    req = proj["requirements"][cell["req_idx"]]
    tid = create_task(proj, req)
    start_worker()                                   # 进程内单线程串行 worker
    QueueWorker.enqueue(tid, "prep")
    t0, stall0 = time.time(), None
    while True:
        time.sleep(5)
        from test_agent.store.db import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT status FROM test_tasks WHERE id=?",
                               (tid,)).fetchone()
        finally:
            conn.close()
        st = row["status"]
        if st in TERMINAL:
            break
        if st == "ready_for_review":
            stall0 = stall0 or time.time()
            if time.time() - stall0 > READY_STALL_S:
                conn = get_conn()
                try:
                    conn.execute("UPDATE test_tasks SET status='failed',"
                                 " error='实验轮：ready_for_review 停等超时（auto 钩子未收敛）',"
                                 " updated_at=datetime('now') WHERE id=?", (tid,))
                    conn.commit()
                finally:
                    conn.close()
                break
        else:
            stall0 = None
        if time.time() - t0 > TASK_TIMEOUT_S:
            conn = get_conn()
            try:
                conn.execute("UPDATE test_tasks SET status='failed',"
                             " error='实验轮：单任务超时（25min）', updated_at=datetime('now')"
                             " WHERE id=?", (tid,))
                conn.commit()
            finally:
                conn.close()
            break
    out = _collect(tid)
    out.update({"lang": cell["lang"], "project": cell["project"],
                "req_idx": cell["req_idx"], "req_title": req["title"],
                "n": cell["n"], "priority": cell["priority"], "retry": retry,
                "elapsed_s": round(time.time() - t0, 1), "requirement": req["requirement"]})
    RESULTS.mkdir(parents=True, exist_ok=True)
    result_path(cell).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def build_cells(manifest: dict, priority: str) -> list[dict]:
    cells = []
    for proj in manifest["projects"]:
        if not proj.get("requirements"):
            continue
        for i in range(5):
            ns = [1, 2, 3] if (proj["representative"] or priority == "P3") else [1]
            if priority == "P1" and not proj["representative"]:
                continue
            if priority == "P2" and proj["representative"]:
                continue
            if priority == "P2":
                ns = [1]
            if priority == "P3":
                ns = [2, 3] if not proj["representative"] else []
            for n in ns:
                cells.append({"lang": proj["lang"], "project": proj["project"],
                              "req_idx": i, "n": n, "priority": priority, "proj": proj})
    return cells


def cmd_run(priority: str, smoke: bool) -> int:
    manifest = _load_manifest()
    api_key = __import__("os").environ.get("DEEPSEEK_API_KEY", "")
    cells = build_cells(manifest, priority)
    if smoke:
        cells = cells[:1]
    RESULTS.mkdir(parents=True, exist_ok=True)
    todo = [c for c in cells if not result_path(c).exists()]
    print(f"[run {priority}] 计划 {len(cells)} 格，断点跳过 {len(cells)-len(todo)}，待跑 {len(todo)}")
    done = fail = consec_fail = 0
    started = time.time()
    for k, cell in enumerate(todo):
        tag = f"{cell['lang']}/{cell['project']}/req{cell['req_idx']}/n{cell['n']}"
        try:
            out = run_cell(cell, api_key)
        except Exception as e:                    # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            if "BudgetExhausted" in msg or "402" in msg or "Insufficient" in msg:
                _ledger_append(f"**预算耗尽停车线** {priority} 于 {tag}：{msg}——停批报告")
                print(f"[BUDGET-STOP] {tag}: {msg}")
                return 2
            # 建任务/驱动层失败：同格重试 1 次
            if cell.get("_retried"):
                _ledger_append(f"DRIVER-FAIL {tag}: {msg}（重试后仍失败，挂起该格）")
                fail += 1
                consec_fail += 1
                continue
            cell["_retried"] = True
            _ledger_append(f"DRIVER-RETRY {tag}: {msg}")
            todo.append(cell)                     # 排到队尾重试一次
            continue
        ok = out["status"] == "done"
        consec_fail = 0 if ok else consec_fail + 1
        done += ok
        fail += not ok
        _ledger_append(f"{priority} {tag} tid={out['task_id']} status={out['status']} "
                       f"cases={out['cases_generated']} pass={out['case_results']['passed']}"
                       f"/{out['case_results']['failed']}/{out['case_results']['error']} "
                       f"rounds={len(out['rounds'])} tokens={out['tokens']} "
                       f"kind={out['attribution']['reported_kind']} retry={out['retry']}")
        print(f"[{k+1}/{len(todo)}] {tag} tid={out['task_id']} {out['status']} "
              f"cases={out['cases_generated']} tokens={out['tokens']}")
        if consec_fail >= 5:
            _ledger_append(f"**停车线：连续 5 任务失败**（{priority}，于 {tag}）——停批诊断")
            print("[FAIL-STOP] 连续 5 任务失败——停批")
            return 1
        n_all = done + fail
        if n_all >= 8 and fail / n_all > 0.30:
            _ledger_append(f"**停车线：失败率>30%**（{fail}/{n_all}，{priority}）——停批诊断")
            print("[FAIL-STOP] 失败率>30%——停批")
            return 1
    print(f"[run {priority}] 完成：done={done} fail={fail} 用时 {round(time.time()-started,1)}s")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="多语言真实项目实验 driver")
    ap.add_argument("cmd", choices=["gen-reqs", "run", "aggregate", "status"])
    ap.add_argument("--priority", default="P1", choices=["P1", "P2", "P3", "ALL"])
    ap.add_argument("--smoke", action="store_true", help="冒烟：只跑 1 格核对形态")
    args = ap.parse_args()
    api_key = __import__("os").environ.get("DEEPSEEK_API_KEY", "")
    if args.cmd == "gen-reqs":
        manifest = _load_manifest()
        for proj in manifest["projects"]:
            if proj.get("requirements"):
                print(f"[skip] {proj['project']} 已有需求（固化不动）")
                continue
            reqs = gen_requirements(api_key, proj)
            proj["requirements"] = reqs
            _save_manifest(manifest)
            print(f"[gen-reqs] {proj['lang']}/{proj['project']}: "
                  + "；".join(r["title"] for r in reqs))
        return 0
    if args.cmd == "run":
        prios = ["P1", "P2", "P3"] if args.priority == "ALL" else [args.priority]
        rc = 0
        for pio in prios:
            rc = cmd_run(pio, args.smoke)
            if rc != 0:
                break
        return rc
    if args.cmd == "status":
        files = sorted(RESULTS.glob("*.json")) if RESULTS.exists() else []
        done = sum(1 for f in files if json.loads(f.read_text(encoding="utf-8"))["status"] == "done")
        print(f"结果 JSON {len(files)} 格，其中 done {done}")
        return 0
    return aggregate()


def aggregate() -> int:
    """聚合三件套：aggregate.md（全量表）/ stability.md（n=3 方差）/ findings.md（诚实结论）。"""
    manifest = _load_manifest()
    files = sorted(RESULTS.glob("*.json"))
    rows = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # —— aggregate.md：语言×项目×需求 全量表 ——
    lines = ["# 聚合表 · 多语言真实项目冒烟（multilang-real-smoke）",
             "", f"> 结果格 {len(rows)} 个（任务级），任务口径=两阶段 auto 全链路（真实库 test_tasks）。"
                 "> 真实项目无缺陷真值：不产 recall，产出=可用性与归因诚实性证据。", "",
             "| 语言 | 项目 | 需求# | n | status | 生成用例 | 通过/失败/错误 | 轮次 | tokens | 归因 kind | env_blocked |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["lang"], x["project"], x["req_idx"], x["n"])):
        cr = r["case_results"]
        lines.append(
            f"| {r['lang']} | {r['project']} | {r['req_idx']} | {r['n']} | {r['status']} "
            f"| {r['cases_generated']} | {cr['passed']}/{cr['failed']}/{cr['error']} "
            f"| {len(r['rounds'])} | {r['tokens']} | {r['attribution']['reported_kind'] or '-'} "
            f"| {r['attribution']['env_blocked_hit']} |")
    (REPORT_DIR / "aggregate.md").write_text(NEWLINE.join(lines), encoding="utf-8")

    # —— stability.md：代表项目 n=3 方差 ——
    rep_rows = [r for r in rows if r["n"] and r["n"] in (1, 2, 3)]
    by_key: dict = {}
    for r in rep_rows:
        if not r["priority"] == "P1":
            continue
        by_key.setdefault((r["lang"], r["project"], r["req_idx"]), []).append(r)
    st = ["# n=3 稳定性（P1 代表项目）", "",
          "| 语言 | 项目 | 需求# | 用例数(n=3) | 最终通过(n=3) | 轮次(n=3) | tokens(n=3) | 终态(n=3) |",
          "|---|---|---|---|---|---|---|---|"]
    for (lang, proj, ri), rs in sorted(by_key.items()):
        rs = sorted(rs, key=lambda x: x["n"])
        if len(rs) < 3:
            continue
        cells = lambda f: ", ".join(str(f(x)) for x in rs)  # noqa: E731
        st.append(f"| {lang} | {proj} | {ri} | {cells(lambda x: x['cases_generated'])} "
                  f"| {cells(lambda x: x['case_results']['passed'])} "
                  f"| {cells(lambda x: len(x['rounds']))} | {cells(lambda x: x['tokens'])} "
                  f"| {cells(lambda x: x['status'])} |")
    (REPORT_DIR / "stability.md").write_text(NEWLINE.join(st), encoding="utf-8")

    # —— findings.md：骨架（数据齐后人工补结论）——
    langs = {}
    for r in rows:
        d = langs.setdefault(r["lang"], {"n": 0, "done": 0, "pass_any": 0, "tokens": 0})
        d["n"] += 1
        d["done"] += r["status"] == "done"
        d["pass_any"] += r["case_results"]["passed"] > 0
        d["tokens"] += r["tokens"]
    fin = ["# 诚实结论 · 多语言真实项目冒烟", "",
           f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')}；数据格 {len(rows)}。", ""]
    for lang, d in sorted(langs.items()):
        fin.append(f"- **{lang}**：{d['n']} 格，done {d['done']}，通过率>0 的格 {d['pass_any']}，"
                   f"总 tokens {d['tokens']}")
    fin += ["", "## 论文口径声明", "",
            "真实项目无缺陷真值——本实验不产 recall；产出=①生成可用性（用例数/通过率/轮次/token）",
            "②归因诚实性（case_defect/product_defect/env 分布与 env_blocked 如实接管）。",
            "与 E9 Python 合成语料基线的形态差异逐条陈述，不美化。"]
    (REPORT_DIR / "findings.md").write_text(NEWLINE.join(fin), encoding="utf-8")
    print(f"[aggregate] 写入 {REPORT_DIR}/{{aggregate,stability,findings}}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
