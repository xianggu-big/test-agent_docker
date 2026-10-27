"""L1 对抗性测试（红队自测）——证据先行：先复现缺陷，修复后回归。

运行前：python -m uvicorn test_agent.api.server:app --port 8000（另一终端/后台）。
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
DB = Path(__file__).resolve().parent.parent / "data" / "test_agent.db"
WS7_DIR = Path(__file__).resolve().parent.parent / "data" / "workspaces" / "task_7"
WS7 = WS7_DIR / "test_imported.py"
results: list[tuple[str, bool, str]] = []


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'DEFECT'}] {name}" + (f"  → {detail}" if not cond else ""))


def main():
    _, dev = call("POST", "/api/login", {"username": "dev", "password": "dev123"})
    _, admin = call("POST", "/api/login", {"username": "admin", "password": "admin123"})
    _, caller = call("POST", "/api/login", {"username": "caller", "password": "caller123"})
    dt, at = dev["token"], admin["token"]

    # A) 越权与注入面
    check("A1 caller 读配置被拒 403", call("GET", "/api/config", token=caller["token"])[0] == 403)
    check("A2 伪造 token 401", call("GET", "/api/tasks", token="fake")[0] == 401)
    check("A3 镜像路径穿越被拦", call("POST", "/api/images/..%2F..%2Fetc/build", {}, token=at)[0] in (404, 405))
    check("A4 缺字段 422", call("POST", "/api/tasks", {}, token=dt)[0] == 422)

    # B) 运行中任务编辑防护
    _, t = call("POST", "/api/tasks", {"repo_url": r"D:\test agent_docker\data\demo\场景B",
                                       "requirement": "对抗测试：运行中编辑防护"}, token=dt)
    tid = t["task_id"]
    call("POST", f"/api/tasks/{tid}/confirm", {}, token=dt)
    call("POST", f"/api/tasks/{tid}/run", {}, token=dt)
    deadline, status = time.time() + 20, "queued"
    while time.time() < deadline:  # 等后台线程把状态推进到执行态
        status = call("GET", f"/api/tasks/{tid}", token=dt)[1].get("status")
        if status != "queued":
            break
        time.sleep(0.3)
    code, _ = call("POST", f"/api/tasks/{tid}/cases",
                   {"name": "运行中插入", "code": "def test_x():\n    assert True"}, token=dt)
    check("B1 运行中插用例被拒 409", code == 409, f"code={code}")
    call("POST", f"/api/tasks/{tid}/stop", {}, token=at)  # 收尾：强停，防真跑一轮 LLM

    # C) 卡死任务恢复（进程被杀 → status 停在执行态 → 任务变砖？）
    _, t2 = call("POST", "/api/tasks", {"repo_url": "local://brick", "requirement": "对抗测试：卡死恢复"}, token=dt)
    tid2 = t2["task_id"]
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE test_tasks SET status='running' WHERE id=?", (tid2,))
    conn.commit()
    conn.close()
    code, _ = call("POST", f"/api/tasks/{tid2}/run", {}, token=dt)
    check("C1 执行态任务拒并发下发 409", code == 409, f"code={code}")
    code, _ = call("POST", f"/api/tasks/{tid2}/stop", {}, token=at)
    status2 = call("GET", f"/api/tasks/{tid2}", token=dt)[1].get("status")
    check("C2 管理员可强制解卡", code == 200 and status2 == "failed", f"code={code} status={status2}")

    # D) 跨脚本重名 → test_imported.py 内 def 重复（用已跑过的任务 7 的工作区，测完自清理）
    if WS7_DIR.exists():
        for _ in range(2):
            call("POST", "/api/tasks/7/cases",
                 {"name": "重复名检测", "code": "def test_dup_name_probe():\n    assert True"}, token=dt)
        n = WS7.read_text(encoding="utf-8").count("def test_dup_name_probe") if WS7.exists() else 0
        check("D1 重复插名自动去重", n <= 1, f"文件中出现 {n} 次 def test_dup_name_probe")
        # 自清理：删探针用例行，再借 toggle 触发文件重建（无 imported 余量则删文件）
        conn = sqlite3.connect(DB)
        conn.execute("""DELETE FROM test_cases WHERE name LIKE 'test_重复名检测%'
                        AND source='imported' AND script_id IN
                        (SELECT id FROM test_scripts WHERE task_id=7)""")
        conn.commit()
        remain = conn.execute(
            """SELECT c.id FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
               WHERE s.task_id=7 AND c.source='imported' LIMIT 1""").fetchone()
        conn.close()
        if remain:
            call("POST", f"/api/cases/{remain[0]}/toggle", {}, token=dt)
            call("POST", f"/api/cases/{remain[0]}/toggle", {}, token=dt)
        elif WS7.exists():
            WS7.unlink()
    else:
        print("[SKIP] D1（task_7 工作区不存在）")

    # E) enabled 启停端点存在性（405=落入静态挂载=端点缺失；404=路由存在但用例号不存在）
    code, _ = call("POST", "/api/cases/999999/toggle", {}, token=dt)
    check("E1 用例启停端点存在", code == 404, f"code={code}")

    print()
    bad = [r for r in results if not r[1]]
    print(f"对抗测试完成：{len(results) - len(bad)} 过 / {len(bad)} 实锤缺陷")
    for name, _, detail in bad:
        print(f"  - {name}: {detail}")
    sys.exit(0)


if __name__ == "__main__":
    main()
