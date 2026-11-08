# -*- coding: utf-8 -*-
"""M7 真机验收 driver（任务卡-LLM模型选择-20261108 §4）：
local 档最小任务全链路 → llm_usage 模型核验 → 切回 deepseek 复跑。
用法：python scripts/verify_llm_profile_task.py <provider> <requirement标签>
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8000"
REPO = "D:/test agent_docker/data/demo/场景A"


def api(path, body=None, token=None, method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if body is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "local"
    token = api("/api/login", {"username": "admin", "password": "admin123"})["token"]

    # 1) 切 profile
    p = api("/api/llm-profile", {"provider": provider}, token)
    print(f"[verify] profile → {p['profile']['provider']}/{p['profile']['model']}", flush=True)

    # 2) 建单阶段最小任务（场景A calc.py）
    t = api("/api/tasks", {
        "repo_url": REPO,
        "requirement": "测试 calc.py 的 add 与 div 函数：加法正常求和；除法除以零应抛 ZeroDivisionError",
        "two_stage": False, "ai_reviewer": "off"}, token)
    tid = t.get("task_id") or t.get("id")
    print(f"[verify] task={tid}", flush=True)
    api(f"/api/tasks/{tid}/confirm", {}, token)
    api(f"/api/tasks/{tid}/run", {}, token)

    # 3) 轮询终态（20min 上限——local 模型慢）
    t0 = time.time()
    while time.time() - t0 < 20 * 60:
        time.sleep(10)
        st = api(f"/api/tasks/{tid}", token=token)
        s = st.get("status") if isinstance(st, dict) else None
        print(f"[verify] status={s} ({int(time.time()-t0)}s)", flush=True)
        if s in ("done", "failed", "cancelled"):
            break
    else:
        print("[verify] 超时", flush=True)
        return 2

    # 4) llm_usage 模型核验（真实库）
    import sqlite3
    from test_agent.store.db import get_conn
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT phase, model, COUNT(*) n FROM llm_calls WHERE task_id=? GROUP BY phase, model",
            (tid,))]
        print("[verify] llm_usage:", json.dumps(rows, ensure_ascii=False), flush=True)
        task = dict(conn.execute("SELECT status, error FROM test_tasks WHERE id=?",
                                 (tid,)).fetchone())
        print("[verify] 终态:", task["status"], "|", (task["error"] or "")[:120], flush=True)
        models = {r["model"] for r in rows}
        ok = all("qwen3-4b" in m for m in models) if provider == "local" else True
        print("[verify] 模型核验:", "PASS" if ok else "FAIL", flush=True)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
