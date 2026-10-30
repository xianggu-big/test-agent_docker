"""E8 四级接力批量驱动（论文 6.8 回填数据源）：demo 项目 → run_task_levels 接力 → 逐轮留档。

用法：
  python scripts/run_e8_levels.py --dry-run          # 只打印跑次矩阵+预估成本，零 LLM
  python scripts/run_e8_levels.py --n 1              # 单轮（成本校准用）
  python scripts/run_e8_levels.py --n 3              # 正式批量（放量前需预算确认）
  python scripts/run_e8_levels.py --full             # 含 L3/L4 leg（LEVELS_L3/L4_ENABLED 门，默认关）

产物：data/reports/e8_r<i>.json（每轮一份：test_levels 各 leg 行+按级 rounds+接力摘要）
口径（诚实边界）：
- 被测对象=data/demo/场景A（L1/L2 接力已真机验证的口径，任务151 同源）；范围默认 L1→L2，
  --full 才尝试 L3/L4（config.LEVELS_L3_ENABLED/LEVELS_L4_ENABLED 全局门，默认关，--full 会提示）。
- demo 项目需含 requirements（httpx 缺失会触发 collect 闸连废——2026-10-30 终验教训，场景A 已补）。
- 失败 leg 如实留档（stop/契约违背是证人语义，不是坏事）。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.levels import run_task_levels  # noqa: E402
from test_agent.store.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "demo" / "场景A"
OUT = ROOT / "data" / "reports"
REQUIREMENT = ("对被测项目做功能测试：输入校验（空值/越界）、正常路径、边界值；"
               "行覆盖率目标 70%。")


def _create_task(conn) -> int:
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(DEMO), "e8-relay-demo")).lastrowid
    return conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, REQUIREMENT, uid)).lastrowid


def _collect(conn, tid: int, summary: dict, t0: float, round_no: int) -> dict:
    legs = [dict(r) for r in conn.execute(
        "SELECT level, strategy, status, verdict, stop_reason, summary, trace_from"
        " FROM test_levels WHERE task_id=? ORDER BY id", (tid,))]
    rounds = [dict(r) for r in conn.execute(
        "SELECT level, round_no, passed, failed, coverage_line, verdict, failure_kind"
        " FROM evaluation_rounds WHERE task_id=? ORDER BY id", (tid,))]
    lc = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens),0) AS t"
                      " FROM llm_calls WHERE task_id=?", (tid,)).fetchone()
    return {"experiment": "E8", "round": round_no, "task_id": tid,
            "levels_requested": summary.get("levels", []) if summary else None,
            "relay_result": summary, "legs": legs, "rounds": rounds,
            "llm": {"calls": lc["c"], "total_tokens": lc["t"]} if lc else None,
            "duration_s": round(time.time() - t0, 1),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    n = int(args[args.index("--n") + 1]) if "--n" in args else 1
    full = "--full" in args
    levels = ("L1", "L2", "L3", "L4") if full else ("L1", "L2")
    if not DEMO.exists():
        print(f"[e8] demo 目录不存在：{DEMO}")
        return 2

    print(f"[e8] 跑次矩阵：{n} 轮接力（levels={levels}，被测 {DEMO.name}）")
    if dry:
        # 成本预估：任务151（L1→L2 接力同口径）实测 11,000-15,000 token 级（未精确复核，标注）
        print(f"[e8] dry-run 预估：单轮 ≈ 12k-25k token（未验证，以 --n 1 实测为准）；"
              f"n={n} 合计 ≈ {n*18}k token ≈ {n*18/35:.2f} 元（1 元≈35k token 折算）")
        print("[e8] dry-run 结束，未调 LLM、未建任务")
        return 0

    if full:
        from test_agent import config
        if not (config.LEVELS_L3_ENABLED or config.LEVELS_L4_ENABLED):
            print("[e8] --full 需 LEVELS_L3_ENABLED/LEVELS_L4_ENABLED（当前默认关）；"
                  "本次仍按 run_task_levels 内部门控执行（L3/L4 将被跳过，如实在 legs 中缺席）")

    conn = get_conn()
    rows = []
    for i in range(1, n + 1):
        tid = _create_task(conn)
        conn.commit()
        print(f"[e8] r{i} → 任务 #{tid} 接力开始 ...")
        t0 = time.time()
        try:
            summary = run_task_levels(tid, levels=levels)
        except Exception as e:
            print(f"[e8] r{i} 任务 #{tid} 异常：{e}")
            summary = None
        row = _collect(conn, tid, summary, t0, i)
        out = OUT / f"e8_r{i}.json"
        out.write_text(json.dumps(row, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        rows.append(row)
        leg_desc = " → ".join(f"{l['level']}:{l['status']}" for l in row["legs"]) or "（无 leg）"
        print(f"[e8] r{i} → {leg_desc} | token {row['llm']} | {out.name}")
    print(f"[e8] 完成 {len(rows)} 轮；任务清单：" + ", ".join(f"#{r['task_id']}" for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
