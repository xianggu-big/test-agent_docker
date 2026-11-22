# -*- coding: utf-8 -*-
"""G7 放行门真机验收（2026-11-20 设计文档 §2）。

验证 A1（bd2e7a5）：auto 任务的 D2 代审循环 fail-closed 后不得自动入队。
手法：真库建 auto 两阶段任务 → 真机跑阶段一（clone/analyze/plan/generate，真 LLM）→
停 ready_for_review → 进程内把停车线 AI_REVIEWER_TOKEN_BUDGET 调至 1（D2 首轮必触顶）
→ post_prep_review_hook → 断言零入队+fail-closed 留痕 → 恢复预算跑收敛对照
（真 review 一轮）→ 断言入队恰好一次 → 清理对照队列行。
零 mock LLM：阶段一出题+对照 review 都是真调用（低价窗合规）。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent / "g7_release_gate.json"
rec = {"gid": "G7", "steps": [], "pass": False}


def step(name, ok, detail):
    rec["steps"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def main():
    from test_agent import config
    from test_agent.store import db
    db.init_db()
    conn = db.get_conn()

    # 1. 真库建 auto 两阶段任务（js-calc 本地目录）
    pid = 280   # js-calc（data/demo/js-calc 本地目录）
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "two_stage,ai_reviewer,requirement_text) VALUES(?,?,0.7,'queued',1,1,'auto',?)",
        (pid, 1, "测试计算器的加法功能和 clamp 边界处理（验收格 G7）")).lastrowid
    conn.commit()
    rec["tid"] = tid
    print(f"任务 #{tid} 已建（auto 两阶段）", flush=True)

    # 2. 真机跑阶段一（真 LLM 出题，数分钟）
    from test_agent.graph.builder import post_prep_review_hook, run_task_prep
    run_task_prep(tid)
    row = conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    step("阶段一真机完成", row["status"] == "ready_for_review", f"status={row['status']}")

    # 3. 停车线调极小 → hook → fail_closed 零入队
    from test_agent.store.queue import QueueWorker
    real_budget = config.AI_REVIEWER_TOKEN_BUDGET
    config.AI_REVIEWER_TOKEN_BUDGET = 1   # D2 首轮必触顶（llm_calls 已有出题消耗）
    try:
        post_prep_review_hook(tid)
    finally:
        config.AI_REVIEWER_TOKEN_BUDGET = real_budget
    qrows = conn.execute("SELECT COUNT(*) AS n FROM exec_queue WHERE task_id=?",
                         (tid,)).fetchone()["n"]
    row = conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
    msgs = [r["content"] for r in conn.execute(
        "SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id=m.session_id "
        "WHERE s.task_id=? ORDER BY m.id DESC LIMIT 3", (tid,))]
    step("fail-closed 零入队", qrows == 0 and row["status"] == "ready_for_review",
         f"exec_queue 行数={qrows} status={row['status']}")
    step("停车线留痕", any("停车线" in m for m in msgs),
         f"最近消息尾={msgs[0][:80] if msgs else '无'}")

    # 4. 收敛对照：恢复预算 → 真 review 一轮 → 收敛则入队恰好一次
    q0 = conn.execute("SELECT COUNT(*) AS n FROM exec_queue WHERE task_id=?",
                      (tid,)).fetchone()["n"]
    post_prep_review_hook(tid)
    time.sleep(1)
    q1 = conn.execute(
        "SELECT COUNT(*) AS n FROM exec_queue WHERE task_id=? AND kind='exec'",
        (tid,)).fetchone()["n"]
    step("收敛对照入队一次", q1 == q0 + 1,
         f"对照后 exec 行 {q0}→{q1}（真 review 一轮，收敛/增补路径如实）")

    # 5. 清理对照队列行（防干扰后续格；任务行保留作审计痕迹）
    conn.execute("DELETE FROM exec_queue WHERE task_id=?", (tid,))
    conn.commit()
    rec["pass"] = all(s["pass"] for s in rec["steps"])
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nG7 判定: {'PASS' if rec['pass'] else 'FAIL'} → {OUT}", flush=True)
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
