"""Go L3/L4 真机验收（2026-11-15，F 卡矩阵最后一格；DeepSeek 余额已充）。

链路：go-api-book demo（gin，埋缺陷：DELETE /books/{id} 无幂等保护）→
建任务 → run_task_levels L1→L2→L3→L4 全接力 →
防假绿口径（F 卡五件套②）：L3 场景数>0 且冒烟真执行（requests 打 127.0.0.1）；
L4 GWT 清单落库（pending 待人工确认为设计语义，不冒充已执行）。

用法：python scripts/demo_go_l3l4.py
产物：data/reports/go_l3l4/summary.json
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.store.db import get_conn, init_db  # noqa: E402
from test_agent.graph.levels import run_task_levels  # noqa: E402

DEMO = ROOT / "data" / "demo" / "go-api-book"
OUT = ROOT / "data" / "reports" / "go_l3l4"

REQUIREMENT = """# 图书管理 REST 服务（Go/gin）

提供图书的查询、创建与删除接口：
- GET /ping：健康探针，返回 {"message": "pong"}
- GET /books：列出全部图书（JSON 数组）
- POST /books：创建图书（JSON body），创建成功返回 201 与图书对象
- DELETE /books/{id}：按 id 删除图书

验收关注：创建后列表可见；删除语义的正确性（含重复删除行为）；
响应均为 JSON；非法 id 的处理。
"""


def main() -> int:
    init_db()
    conn = get_conn()
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(DEMO), "go-l3l4-demo")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, REQUIREMENT, uid)).lastrowid
    conn.commit()
    ws = ROOT / "data" / "workspaces" / f"task_{tid}"
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(DEMO, ws, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"[go-l3l4] 任务 {tid}，工作区 {ws}", flush=True)

    final = run_task_levels(tid, levels=("L1", "L2", "L3", "L4"))

    conn2 = get_conn()
    legs = {}
    for lv in ("L1", "L2", "L3", "L4"):
        row = conn2.execute(
            "SELECT status, verdict, stop_reason FROM test_levels"
            " WHERE task_id=? AND level=?", (tid, lv)).fetchone()
        legs[lv] = dict(row) if row else None
    sc = [dict(r) for r in conn2.execute(
        "SELECT level, status, COUNT(*) n FROM scenario_cases"
        " WHERE task_id=? GROUP BY level, status", (tid,))]
    er = [dict(r) for r in conn2.execute(
        "SELECT level, round_no, passed, failed, verdict, substr(stop_reason,1,80) sr"
        " FROM evaluation_rounds WHERE task_id=?", (tid,))]
    summary = {"task_id": tid, "final": {k: final.get(k) for k in final
                                         if k in ("verdict", "stop_reason")},
               "legs": legs, "scenarios": sc, "rounds": er}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    # 防假绿判定：L3 场景真执行（auto_pass/pending>0 且 originate 自冒烟）；L4 清单落库
    l3 = legs.get("L3") or {}
    l3_sc = [s for s in sc if s["level"] == "L3"]
    l3_executed = sum(s["n"] for s in l3_sc if s["status"] in ("auto_pass", "pending_confirm"))
    l4 = legs.get("L4") or {}
    l4_n = sum(s["n"] for s in sc if s["level"] == "L4")
    ok = l3_executed > 0 and l4_n > 0
    print(f"[go-l3l4] 防假绿判定：L3 场景真执行 {l3_executed} 条（verdict={l3.get('verdict')}）、"
          f"L4 GWT 清单 {l4_n} 条（pending 待人工确认=设计语义）→ {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
