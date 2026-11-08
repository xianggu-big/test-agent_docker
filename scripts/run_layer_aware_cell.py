"""层位感知对照格（2026-11-08 实验轮，用户授权 run_task 全链路窗口）。

单变量设计：与 653（L2 口语形态格）唯一差异 = MTA_LAYER_AWARE=1（层位覆盖纪律+
Go httptest HTTP 层骨架注入）。其余逐项复刻 653：L2 口语需求原文（从 653 库内
requirement_text 剥前缀，逐字节一致）+ MTA_UNDERSTAND=1 + two_stage auto 代审。

补 653/648 已知症状的放行替身：D2 代审决策收敛（review_decisions 计数稳定）后
exec 未自动入队 → 检测收敛主动 enqueue exec（653 手动 POST /run 的等效动作）。

产物：data/experiments/multilang/results/go-rest-api-l2-oral_layer1.json
红线：只写实验产物，不改 test_agent/ 包内任何文件；不碰 E9/E10 产物。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 旗标必须在 test_agent.config 首次 import 前就位（config 模块级读取环境变量）
os.environ.setdefault("MTA_LAYER_AWARE", "1")
os.environ.setdefault("MTA_UNDERSTAND", "1")

from test_agent import config  # noqa: E402

assert config.LAYER_AWARE_ENABLED is True, "MTA_LAYER_AWARE 未生效"
assert config.UNDERSTAND_ENABLED is True, "MTA_UNDERSTAND 未生效"

sys.path.insert(0, str(ROOT / "scripts"))
from run_multilang_real import RESULTS, _collect, create_task  # noqa: E402

PROJ = {"project": "go-rest-api-l2-oral-layer1", "lang": "go",
        "local_path": r"D:\go-rest-api-master（go_test1）\go-rest-api-master",
        "description": "REST 专辑服务（gin），652/653 同项目层位感知对照格"}
REQ_TITLE = "L2 口语形态·层位感知格"
TASK_TIMEOUT_S = 35 * 60          # 653 全程 27min，层位格留余量
STABLE_S = 20                     # 决策计数稳定判定窗
RELEASE_GRACE_S = 60              # ready_for_review 最短观察期


def load_l2_requirement() -> str:
    """从 653 库内 requirement_text 剥双层前缀，保证与对照格需求逐字节一致。"""
    from test_agent.store.db import get_conn
    conn = get_conn()
    try:
        t = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=653"
                         ).fetchone()
        text = t["requirement_text"]
        assert text.startswith("[multilang-real] "), "653 前缀形态变了，人工核对"
        return text.split("]", 2)[2].strip()
    finally:
        conn.close()


def _counts(tid: int) -> tuple[str, int, bool, str]:
    """(状态, 代审决策数, exec 已入队否, prep 行状态)。读撞锁=False 空轮询。"""
    from test_agent.store.db import get_conn
    try:
        conn = get_conn()
        try:
            st = conn.execute("SELECT status FROM test_tasks WHERE id=?",
                              (tid,)).fetchone()["status"]
            n = conn.execute("SELECT COUNT(*) c FROM review_decisions WHERE task_id=?",
                             (tid,)).fetchone()["c"]
            q = conn.execute("SELECT COUNT(*) c FROM exec_queue WHERE task_id=? AND kind='exec'",
                             (tid,)).fetchone()["c"]
            pr = conn.execute("SELECT status FROM exec_queue WHERE task_id=? AND kind='prep'",
                              (tid,)).fetchone()
            return st, n, q > 0, (pr["status"] if pr else "")
        finally:
            conn.close()
    except Exception:
        return "", 0, False, ""


def _fail_task(tid: int, msg: str) -> None:
    """终态写入带重试（server 8000 跨进程写锁是间歇窗口，重试必进）。"""
    import sqlite3
    from test_agent.store.db import get_conn
    for _ in range(30):
        try:
            conn = get_conn()
            try:
                conn.execute("UPDATE test_tasks SET status='failed', error=?,"
                             " updated_at=datetime('now') WHERE id=?", (msg, tid))
                conn.commit()
            finally:
                conn.close()
            return
        except sqlite3.OperationalError:
            time.sleep(3)


def main() -> int:
    from test_agent.store.queue import QueueWorker, start_worker

    req = {"title": REQ_TITLE, "requirement": load_l2_requirement()}
    tid = create_task(PROJ, req)
    print(f"[cell] task={tid} LAYER_AWARE=1 UNDERSTAND=1 l2_oral=逐字节复刻653",
          flush=True)
    start_worker()
    QueueWorker.enqueue(tid, "prep")

    t0, stall0, released = time.time(), None, False
    last_dec, stable_since = -1, None
    while True:
        time.sleep(5)
        st, n_dec, q_exec, prep_st = _counts(tid)
        if not st:
            continue                       # 读撞锁空轮询（server 已关，正常不会发生）
        if st in ("done", "failed", "cancelled"):
            break
        if st == "ready_for_review":
            stall0 = stall0 or time.time()
            # 放行替身（669 乱局修正）：等 prep 行 done（worker 空闲）再 enqueue——
            # D2 增补写活跃时放行=与 gen_tests 写竞争→写锁雪崩+止血与活跃写打架。
            # prep done 后 worker 空→enqueue 无竞争。撞锁重试逻辑保留（幂等）。
            if n_dec > 0:
                if n_dec != last_dec:
                    last_dec, stable_since = n_dec, time.time()
                elif (not released and not q_exec and prep_st == "done"
                      and time.time() - stable_since > STABLE_S):
                    import sqlite3
                    try:
                        QueueWorker.enqueue(tid, "exec")
                        released = True
                        print(f"[cell] 放行替身：exec 已入队（decisions={n_dec} 收敛+prep done）",
                              flush=True)
                    except sqlite3.OperationalError:
                        print("[cell] enqueue 撞锁，下轮重试", flush=True)
            if time.time() - stall0 > 12 * 60 and not released:
                _fail_task(tid, "实验轮：ready_for_review 停等超时（D2 未收敛）")
                break
        else:
            stall0, stable_since = None, None
        if time.time() - t0 > TASK_TIMEOUT_S:
            _fail_task(tid, "实验轮：单任务超时（35min）")
            break

    res = _collect(tid)
    out = {"cell": "go-rest-api-l2-oral_layer1",
           "design": "单变量=LAYER_AWARE=1，其余与 653 逐项一致（L2 口语需求原文/"
                     "understand/auto 代审）",
           "flags": {"layer_aware": True, "understand": True}, **res}
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / "go-rest-api-l2-oral_layer1.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[cell] 终态:", json.dumps({k: out[k] for k in
          ("task_id", "status", "cases_generated", "case_results", "tokens")},
          ensure_ascii=False), flush=True)
    print("[cell] 结果:", p, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
