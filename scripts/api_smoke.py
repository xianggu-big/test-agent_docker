"""L1 API 集成冒烟（stdlib only，不起前端）——阶段 2 的自动化验证。

覆盖：登录 / 角色守卫 / 新建→确认→下发→轮询→报告 / 用例插入 / 镜像接口。
运行前：python -m uvicorn test_agent.api.server:app --port 8000（另一终端）。
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PASSED: list[str] = []


def call(method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name: str, cond: bool, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")
    if not cond:
        print("SMOKE FAILED"); sys.exit(1)


def main() -> None:
    # 1) 未登录访问受保护接口 → 401
    code, _ = call("GET", "/api/tasks")
    check("未登录 401", code == 401)

    # 2) 错误口令 401
    code, _ = call("POST", "/api/login", {"username": "dev", "password": "wrong"})
    check("错误口令 401", code == 401)

    # 3) dev 登录
    code, dev = call("POST", "/api/login", {"username": "dev", "password": "dev123"})
    check("dev 登录", code == 200 and dev.get("role") == "developer", str(dev))
    dt = dev["token"]

    # 4) caller 权限边界：可建任务、不可确认
    code, caller = call("POST", "/api/login", {"username": "caller", "password": "caller123"})
    check("caller 登录", code == 200)
    ct = caller["token"]
    code, t = call("POST", "/api/tasks", {"repo_url": "local://smoke", "requirement": "权限冒烟"}, token=ct)
    check("caller 可新建任务", code == 200, f"{code} {t}")
    code, _ = call("POST", f"/api/tasks/{t['task_id']}/confirm", {}, token=ct)
    check("caller 不可确认 403", code == 403)

    # 5) 主链路：新建 → 确认 → 下发 → 轮询至终态
    code, t = call("POST", "/api/tasks", {
        "repo_url": r"D:\test agent_docker\data\demo\场景B",
        "requirement": "对 add 函数做功能测试：验证正常相加行为"}, token=dt)
    check("dev 新建任务", code == 200, f"{code} {t}")
    tid = t["task_id"]
    code, _ = call("POST", f"/api/tasks/{tid}/confirm", {}, token=dt)
    check("dev 确认", code == 200)
    code, _ = call("POST", f"/api/tasks/{tid}/run", {}, token=dt)
    check("dev 下发", code == 200)

    deadline, status = time.time() + 240, None
    while time.time() < deadline:
        code, d = call("GET", f"/api/tasks/{tid}", token=dt)
        status = d.get("status")
        if status in ("done", "failed", "cancelled"):
            break
        time.sleep(3)
    check(f"任务终态（实际 {status}）", status == "done")

    # 6) 报告与日志
    code, reports = call("GET", f"/api/tasks/{tid}/report", token=dt)
    check("报告非空且通过率 100%", code == 200 and reports and reports[0]["pass_rate"] == 1.0, str(reports[:1]))
    code, logs = call("GET", f"/api/tasks/{tid}/logs", token=dt)
    check("会话日志非空", code == 200 and len(logs) >= 4, f"n={len(logs)}")

    # 7) 人工插用例（写工作区 + 落库 + nodeid 规整）
    code, c = call("POST", f"/api/tasks/{tid}/cases", {
        "name": "冒烟插入用例", "method": "等价类划分",
        "code": "def test_smoke_insert():\n    from calc import add\n    assert add(2, 3) == 5"}, token=dt)
    check("插入用例", code == 200 and c.get("case_name") == "test_冒烟插入用例", str(c))
    code, cases = call("GET", f"/api/tasks/{tid}/cases", token=dt)
    check("用例清单含插入项", any(x["name"] == "test_冒烟插入用例" for x in cases["cases"]))

    # 8) 镜像接口
    code, imgs = call("GET", "/api/images", token=dt)
    check("镜像状态", code == 200 and imgs and all(i["ready"] for i in imgs), str(imgs))

    print(f"\nAPI SMOKE OK：{len(PASSED)} 项全过")


if __name__ == "__main__":
    main()
