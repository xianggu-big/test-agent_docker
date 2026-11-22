"""G8 竞态真机格 v2（2026-11-21 设计稿 docs/G8长链占位格设计-20261121.md 实施）。

v1 miss 根因（三跑全 miss 取证）：占位格（js-calc 80s 快链）在竞态格阶段一（~80s）
期间就跑完——竞态格放行阶段二时 worker 空闲 → 秒认领 → stop@3s 已 running（按设计
409）。v2 三改：
1. **占位格换 java-multimodule-slow**（单阶段全链 ≈4 分钟占住 worker，mvn 慢点
   45s 测试内 Thread.sleep 离线零插件——真容器验证 111s EXIT=0 在案）；
2. **时序重排**：竞态格×3 的阶段一在占位格放行前先出好题（ready_for_review），
   占位格阶段二占住 worker 后，期间依次放行三档竞态格；
3. **三档窗口试探 1s/3s/8s**（单档三跑全 miss 教训：概率窗口多档采样）——
   判据：任一档 stop 成功（cancelled 终态+零执行）即 PASS（与原判据同源）；
   对照=占位格本身跑完 done（健康格证明，不再单开对照格）。

前置：server 活+Docker 活+验收账号 fixverify（developer）；预算 ≈¥0.2-0.3
（3×js-calc 出题 + 1×java-slow 全链 + 占位/竞态 stop 零 token）。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent / "g8_cancel_race.json"
API = "http://127.0.0.1:8000"
REPO_SLOW = r"D:\test agent_docker\data\demo\java-multimodule-slow"
REPO_JS = r"D:\test agent_docker\data\demo\js-calc"
DELAYS = [1, 3, 8]


def _token():
    import httpx
    r = httpx.post(f"{API}/api/login", json={"username": "fixverify",
                                             "password": "Fv#20261120"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _mk_task(tk, repo, requirement, two_stage=True):
    import httpx
    r = httpx.post(f"{API}/api/tasks",
                   json={"repo_url": repo, "requirement": requirement,
                         "two_stage": two_stage},
                   headers={"Authorization": f"Bearer {tk}"}, timeout=10)
    r.raise_for_status()
    return r.json()["task_id"]


def _wait_status(tk, tid, want, timeout_s=900):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro",
                            uri=True)
        row = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
        c.close()
        if row and row[0] == want:
            return row[0]
        time.sleep(3)
    return row[0] if row else "timeout"


def _q_status(tid):
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro",
                        uri=True)
    qstat = [r[0] for r in c.execute(
        "SELECT status FROM exec_queue WHERE task_id=?", (tid,))]
    tstat = c.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()[0]
    c.close()
    return tstat, qstat


def main():
    import httpx
    tk = _token()
    H = {"Authorization": f"Bearer {tk}"}
    rec = {"delays": DELAYS, "steps": []}

    def step(name, ok, detail):
        rec["steps"].append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # —— 1. 三竞态格阶段一出题（prep worker 空闲时串行出好，停 ready_for_review） ——
    race_tids = []
    for i in range(len(DELAYS)):
        tid = _mk_task(tk, REPO_JS,
                       f"测试计算器 clamp 边界（G8 v2 竞态格第 {i+1} 档）")
        httpx.post(f"{API}/api/tasks/{tid}/confirm", headers=H, timeout=10)
        st = _wait_status(tk, tid, "ready_for_review", timeout_s=600)
        step(f"竞态格{i+1} 阶段一出题", st == "ready_for_review", f"tid={tid} status={st}")
        race_tids.append(tid)

    # —— 2. 占位格：java-slow 单阶段全链（占住 worker ≈4 分钟） ——
    blocker = _mk_task(tk, REPO_SLOW,
                       "测试多模块项目的核心功能（G8 v2 占位格：java 慢链堵住单 worker）",
                       two_stage=False)
    rec["tid_blocker"] = blocker
    httpx.post(f"{API}/api/tasks/{blocker}/confirm", headers=H, timeout=10)
    httpx.post(f"{API}/api/tasks/{blocker}/run", headers=H, timeout=10)
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro",
                        uri=True)
    for _ in range(60):        # 等 worker 认领占位格（exec_queue running=占住）
        q = c.execute("SELECT status FROM exec_queue WHERE task_id=?",
                      (blocker,)).fetchone()
        if q and q[0] == "running":
            break
        time.sleep(2)
    c.close()
    step("占位格已认领 running", q and q[0] == "running",
         f"tid={blocker} 队列={q and q[0]}（java-slow 全链 ≈4 分钟窗口）")

    # —— 3. 三档竞态：放行 + sleep 档位 + stop（窗口内依次做） ——
    any_hit = False
    for tid, delay in zip(race_tids, DELAYS):
        r = httpx.post(f"{API}/api/tasks/{tid}/run", headers=H, timeout=10)
        time.sleep(delay)
        r2 = httpx.post(f"{API}/api/tasks/{tid}/stop", headers=H, timeout=10)
        body = r2.json()
        tstat, qstat = _q_status(tid)
        hit = body.get("stopped") is True and tstat == "cancelled" \
            and all(s in ("cancelled", "failed") for s in qstat)
        step(f"竞态 stop@{delay}s", hit,
             f"stopped={body.get('stopped')} 任务={tstat} 队列={qstat} "
             f"http={r2.status_code}")
        any_hit = any_hit or hit

    # —— 4. 对照：占位格本身跑到终态（健康格证明——java-slow 链路全通） ——
    st = _wait_status(tk, blocker, "done", timeout_s=900)
    step("占位格链路健康（done）", st == "done",
         f"tid={blocker} 终态={st}（java 多模块 mvn+LLM 全链）")

    n_pass = sum(1 for s_ in rec["steps"] if s_["pass"])
    rec["verdict"] = "PASS" if (any_hit and st == "done") else "FAIL"
    rec["summary"] = (f"竞态制造={'成功' if any_hit else '未制造'}（三档 {'、'.join(map(str, DELAYS))}s）；"
                      f"占位格健康={'是' if st == 'done' else '否'}")
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nG8 v2 判定: {rec['verdict']} —— {rec['summary']} → {OUT}", flush=True)
    sys.exit(0 if rec["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
