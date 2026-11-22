# -*- coding: utf-8 -*-
"""G8 取消竞态真机验收（2026-11-20 设计文档 §2）+ 全流程健康对照格。

验证 A4（f7203f0）：放行入队后立即停止 → 零执行。
对照格：同需求第二任务正常放行跑完全链（provision→run→evaluate→report），
同时证明修复后全流程管道健康。真 LLM（低价窗）。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent / "g8_cancel_race.json"
API = "http://127.0.0.1:8000"


def _token():
    import httpx
    r = httpx.post(f"{API}/api/login", json={"username": "fixverify",
                                             "password": "Fv#20261120"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _mk_task(tk, requirement):
    import httpx
    r = httpx.post(f"{API}/api/tasks",
                   json={"repo_url": r"D:\test agent_docker\data\demo\js-calc",
                         "requirement": requirement, "two_stage": True},
                   headers={"Authorization": f"Bearer {tk}"}, timeout=10)
    r.raise_for_status()
    return r.json()["task_id"]


def _wait_status(tk, tid, want, timeout_s=900):
    import sqlite3
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
        row = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
        c.close()
        if row and row[0] == want:
            return row[0]
        time.sleep(5)
    return (row[0] if row else "none")


def main():
    import httpx
    tk = _token()
    rec = {"gid": "G8", "steps": [], "pass": False}

    def step(name, ok, detail):
        rec["steps"].append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # 1. 竞态格：先放一个占位格堵住 worker（否则放行即被认领，stop 永远赶不上——
    # 2026-11-20 首跑实锤：3s 后任务已 reporting，stop 按设计 409 不支持运行中强停；
    # A4 的"stop 先取消则认领失败"分支必须让目标格停在 queued 才能制造）
    blocker = _mk_task(tk, "测试计算器除法功能（验收格 G8 占位格：堵住单 worker 让竞态格停在 queued）")
    rec["tid_blocker"] = blocker
    httpx.post(f"{API}/api/tasks/{blocker}/confirm", headers={"Authorization": f"Bearer {tk}"},
               timeout=10)
    _wait_status(tk, blocker, "ready_for_review")
    httpx.post(f"{API}/api/tasks/{blocker}/run", headers={"Authorization": f"Bearer {tk}"},
               timeout=10)
    import sqlite3 as _sq
    _c = _sq.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    for _ in range(30):        # 等 worker 认领占位格（队列 running=占住）
        q = _c.execute("SELECT status FROM exec_queue WHERE task_id=?", (blocker,)).fetchone()
        if q and q[0] == "running":
            break
        time.sleep(2)
    _c.close()

    tid = _mk_task(tk, "测试计算器乘法功能（验收格 G8 竞态格）")
    rec["tid_race"] = tid
    httpx.post(f"{API}/api/tasks/{tid}/confirm", headers={"Authorization": f"Bearer {tk}"},
               timeout=10)
    st = _wait_status(tk, tid, "ready_for_review")
    step("阶段一完成", st == "ready_for_review", f"status={st}")
    r = httpx.post(f"{API}/api/tasks/{tid}/run", headers={"Authorization": f"Bearer {tk}"},
                   timeout=10)
    time.sleep(3)
    r2 = httpx.post(f"{API}/api/tasks/{tid}/stop", headers={"Authorization": f"Bearer {tk}"},
                    timeout=10)
    body = r2.json()
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    tstat = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()[0]
    qstat = [row[0] for row in c.execute("SELECT status FROM exec_queue WHERE task_id=?", (tid,))]
    c.close()
    step("stop 后零执行", body.get("stopped") is True and tstat == "cancelled"
         and all(s in ("cancelled", "failed") for s in qstat),
         f"stopped={body.get('stopped')} 任务={tstat} 队列={qstat}")

    # 2. 对照格：正常放行跑完全链（全流程管道健康）
    tid2 = _mk_task(tk, "测试计算器乘法功能（验收格 G8 对照格）")
    rec["tid_control"] = tid2
    httpx.post(f"{API}/api/tasks/{tid2}/confirm", headers={"Authorization": f"Bearer {tk}"},
               timeout=10)
    _wait_status(tk, tid2, "ready_for_review")
    httpx.post(f"{API}/api/tasks/{tid2}/run", headers={"Authorization": f"Bearer {tk}"},
               timeout=10)
    final = _wait_status(tk, tid2, None, timeout_s=1500)   # 等终态（轮询内判终态）
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    tstat2 = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid2,)).fetchone()[0]
    err2 = c.execute("SELECT substr(error,1,60) FROM test_tasks WHERE id=?", (tid2,)).fetchone()[0]
    c.close()
    healthy = tstat2 in ("done", "failed") and (err2 is None or "402" not in err2)
    step("对照格全链健康", healthy,
         f"终态={tstat2}（done=全绿 / failed+疑似产品缺陷=真检出均健康）error={err2}")
    rec["pass"] = all(s["pass"] for s in rec["steps"])
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nG8 判定: {'PASS' if rec['pass'] else 'FAIL'} → {OUT}", flush=True)
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
