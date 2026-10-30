"""E7 跨级溯源命中率驱动（corpus-003）：建任务 → L1/L2/L3 接力 → 逐场景建链 → 比对真值。

用法：
  python scripts/run_e7_corpus003.py [--n 1] [--no-preflight]
产物：data/corpus/corpus-003/e7_task_<tid>.json（每个任务一份，n 次各自留档）
口径（v1，诚实边界）：
- 代码位置=函数级候选集（call_edges 深度 ≤3），不是精确缺陷行；
- 命中=真缺陷 file::function 出现在任一失败场景的链 CODE 节点；
- rank=该位置在其所属链 CODE 节点中的序号（1 起，越小越靠前）；
- decoy 不计入分子分母。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402
from test_agent.graph.levels import run_task_levels  # noqa: E402
from test_agent.skills.traceability import build_task_traces  # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus" / "corpus-003"


def _create_task(conn, requirement: str) -> int:
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(CORPUS / "project"), "e7-corpus-003")).lastrowid
    return conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid


def _score(conn, task_id: int, gt: dict) -> dict:
    chains = build_task_traces(conn, task_id)
    real = [d for d in gt["planted_defects"] if not d.get("decoy")]
    # 每个真缺陷找最相似的失败场景链（2-gram），再看该链 CODE 候选里有没有它
    from test_agent.skills.traceability import _grams, _scenario_text

    def overlap(a: str, b: str) -> float:
        ag = _grams(a)
        return len(ag & _grams(b)) / len(ag) if ag else 0.0

    rows = []
    hits = 0
    for d in real:
        dtext = f"{d['description']} {d['expected_assertion']}"
        best = max(chains, key=lambda c: overlap(
            dtext, _scenario_text(c["trigger"]))) if chains else None
        loc = (d.get("file"), d.get("function"))
        code = [n for n in (best or {}).get("nodes", []) if n["level"] == "CODE"]
        # call_edges 存 qualname（app.cancel_booking），真值按函数名（cancel_booking）
        found = [i for i, n in enumerate(code, 1)
                 if (n.get("file"), (n.get("function") or "").split(".")[-1]) == loc]
        hit = bool(found)
        hits += int(hit)
        rows.append({"defect": d["id"], "location": f"{loc[0]}::{loc[1]}",
                     "matched_scenario": (best or {}).get("scenario_id"),
                     "matched_title": ((best or {}).get("trigger") or {}).get("title"),
                     "hit": hit, "rank": found[0] if found else None,
                     "candidates": len(code)})
    return {"task_id": task_id, "chains": len(chains),
            "defects_real": len(real), "hits": hits,
            "hit_rate": round(hits / len(real), 4) if real else None,
            "codes": len({(n.get("file"), n.get("function"))
                          for c in chains for n in c["nodes"] if n["level"] == "CODE"}),
            "details": rows,
            "chain_gaps": {c["scenario_id"]: c["gaps"] for c in chains if c["gaps"]}}


def main() -> int:
    args = sys.argv[1:]
    n = int(args[args.index("--n") + 1]) if "--n" in args else 1
    if "--no-preflight" not in args:
        from test_agent.sandbox import preflight
        r = preflight.ensure(auto_repair=True)
        print("[e7] 环境：", preflight.summary(r))
        if not r.get("ready"):
            print("[e7] 环境未就绪，终止（如实上报，不伪造跑数）")
            return 1
    init_db()
    conn = get_conn()
    requirement = (CORPUS / "project" / "REQUIREMENT.md").read_text(encoding="utf-8")
    gt = json.loads((CORPUS / "ground_truth.json").read_text(encoding="utf-8"))
    old_l3 = config.LEVELS_L3_ENABLED
    config.LEVELS_L3_ENABLED = True
    config.LEVELS_L4_ENABLED = False
    results = []
    try:
        for i in range(n):
            tid = _create_task(conn, requirement)
            conn.commit()
            print(f"[e7] run {i + 1}/{n} task={tid} L1→L2→L3")
            error = None
            try:
                run_task_levels(tid, ("L1", "L2", "L3"))
            except Exception as e:  # noqa: BLE001 —— 跑数失败留痕不中断后续
                error = f"{type(e).__name__}: {e}"[:300]
                print(f"[e7] task {tid} 异常：{error}")
            res = _score(conn, tid, gt)
            res["run"] = i + 1
            res["status"] = "blocked" if error else "ok"
            if error:
                res["error"] = error
                res["note"] = "跑数失败（如 LLM 402/环境）——不得当作命中率 0 的实验结果"
            out = CORPUS / f"e7_task_{tid}.json"
            out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(res, ensure_ascii=False))
            results.append(res)
    finally:
        config.LEVELS_L3_ENABLED = old_l3
    ok = [r for r in results if r.get("status") == "ok"]
    print(f"[e7] n={n} 有效跑数={len(ok)} 命中率均值="
          f"{round(sum(r['hit_rate'] for r in ok) / len(ok), 4) if ok else None}"
          "（blocked 不计入均值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
