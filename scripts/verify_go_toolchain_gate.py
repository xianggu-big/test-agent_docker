# -*- coding: utf-8 -*-
"""C 真机验证：Go 工具链版本闸（负例 go1.27 应诚实拦 + 正例 gin 应放行）。

负例走完整任务链路（provision exec `go version` 对比 go.mod 声明 → 不符即 stop），
核心验收：①10 秒级失败而非三轮空转 ②停止原因含所需版本/镜像实况/处理办法
③agent_messages 有 [provision·toolchain] 行 ④evaluation_rounds 无 repair_env 轮次。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
BASE = "http://127.0.0.1:8000"


def api(path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def run_task(repo: str, requirement: str) -> int:
    token = api("/api/login", {"username": "admin", "password": "admin123"})["token"]
    t = api("/api/tasks", {"repo_url": repo, "requirement": requirement,
                           "two_stage": False, "ai_reviewer": "off"}, token)
    tid = t.get("task_id") or t.get("id")
    api(f"/api/tasks/{tid}/confirm", {}, token)
    api(f"/api/tasks/{tid}/run", {}, token)
    return tid


def wait(tid: int, limit_s: int = 15 * 60) -> tuple[str, int]:
    token = api("/api/login", {"username": "admin", "password": "admin123"})["token"]
    t0 = time.time()
    while time.time() - t0 < limit_s:
        time.sleep(5)
        st = api(f"/api/tasks/{tid}", token=token).get("status")
        if st in ("done", "failed", "cancelled"):
            return st, int(time.time() - t0)
    return "timeout", int(time.time() - t0)


def report(tid: int) -> dict:
    from test_agent.store.db import get_conn
    conn = get_conn()
    try:
        task = conn.execute("SELECT status, substr(coalesce(error,''),1,220) e, "
                            "created_at, updated_at FROM test_tasks WHERE id=?",
                            (tid,)).fetchone()
        msgs = [r[0] for r in conn.execute(
            """SELECT m.content FROM agent_messages m JOIN agent_sessions s
               ON s.id=m.session_id WHERE s.task_id=? ORDER BY m.id""", (tid,))]
        rounds = [tuple(r) for r in conn.execute(
            "SELECT round_no, passed, failed, verdict, failure_kind FROM evaluation_rounds "
            "WHERE task_id=?", (tid,))]
        return {"status": task[0], "error": task[1], "messages": msgs, "rounds": rounds,
                "created_at": task[2], "updated_at": task[3]}
    finally:
        conn.close()


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "neg"
    if which == "neg":
        tid = run_task("D:/test agent_docker/data/demo/go-ver-gate",
                       "测试 Add 函数：1+2 应得 3")
        print(f"[gate] 负例任务 {tid} 已建，等待终态（预期 10-60s 内诚实 stop）", flush=True)
    else:
        tid = run_task(r"D:\gin-master（go_test2）\gin-master",
                       "测试一些基础功能")
        print(f"[gate] 正例任务 {tid} 已建（gin 全量 go test，耗时长）", flush=True)
    st, secs = wait(tid)
    rep = report(tid)
    print(f"\n[gate] task={tid} status={st} 用时={secs}s")
    print(f"[gate] 停止原因: {rep['error']}")
    print(f"[gate] 轮次: {rep['rounds']}")
    tool = [m for m in rep["messages"] if "provision·toolchain" in m]
    print(f"[gate] toolchain 消息 {len(tool)} 条:")
    for m in tool:
        print("   -", m[:240])
    if which == "neg":
        ok = (st == "failed" and "工具链版本不足" in (rep["error"] or "")
              and bool(tool) and not rep["rounds"] and secs < 600)
        print(f"\n[gate] 负例验收: {'PASS' if ok else 'FAIL'}"
              "（期望：failed+原因含版本不足+有 toolchain 留痕+零 repair 轮次+快速）")
        return 0 if ok else 1
    # 正例：gin 真跑了（有用例结果行即证明开考，不再 0/0 秒失败）
    from test_agent.store.db import get_conn
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM test_case_results WHERE task_id=?", (tid,)).fetchone()[0]
    conn.close()
    ok = n > 0 and not any("工具链版本不足" in m for m in rep["messages"])
    print(f"\n[gate] 正例验收: {'PASS' if ok else 'FAIL'}"
          f"（用例结果行 {n}；期望 >0 且无版本拦截）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
