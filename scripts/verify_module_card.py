"""任务卡-E 验收：报告页「模块画像与策略建议」卡真浏览器可见性验证（2026-11-10）。

tmp 库 + 临时端口（不碰生产库与 8000 服务）：注册临时用户 → 种项目/任务 →
对 data/demo/multi-module-demo 跑 analyze_modules（零 LLM）并落库 → 线程内起
uvicorn:8899 → Playwright 无头浏览器登录态进入 #/reports → 断言卡片文本与
规则一致 → 截图 data/reports/e_module_card.png。

用法：python scripts/verify_module_card.py
"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP = ROOT / "data" / "reports" / "e_card_tmp"
TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "t.db"
if DB.exists():
    DB.unlink()
config.DB_PATH = DB                      # 先于 server 导入重定向（get_conn 调用时读取）

from test_agent.api import auth  # noqa: E402
from test_agent.skills.module_profile import analyze_modules, persist  # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402

PORT = 8899
DEMO = ROOT / "data" / "demo" / "multi-module-demo"
USER, PW = "e2e_card", "pw-e2e-Card123"


def _seed() -> int:
    init_db(DB)
    assert auth.register(USER, PW), "注册失败"
    conn = get_conn(DB)
    uid = conn.execute("SELECT id FROM users WHERE username=?", (USER,)).fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES(?,?,?)",
                       (uid, str(DEMO), "multi-module-demo")).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text, confirmed, confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, "模块画像验收", uid)).lastrowid
    conn.commit()
    rep = analyze_modules(DEMO, "python", llm_fn=None)
    n = persist(conn, pid, rep)
    conn.close()
    print(f"[seed] task={tid} project={pid} module_profiles={n}")
    return tid


def _serve() -> None:
    import uvicorn
    from test_agent.api.server import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def main() -> int:
    tid = _seed()
    threading.Thread(target=_serve, daemon=True).start()
    for _ in range(40):                  # 等服务就绪
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
            break
        except Exception:                # noqa: BLE001
            time.sleep(0.25)
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/login",
        data=json.dumps({"username": USER, "password": PW}).encode(),
        headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, timeout=5).read())["token"]

    from playwright.sync_api import sync_playwright
    checks = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{PORT}/")
        page.evaluate(f"localStorage.setItem('token', {json.dumps(token)})")
        page.goto(f"http://127.0.0.1:{PORT}/#/reports")
        page.reload()
        page.wait_for_selector("text=模块画像与策略建议", timeout=15000, state="attached")
        page.evaluate("document.querySelectorAll('details.v-details').forEach(d => d.open = true)")
        html = page.inner_html("#rp-overview")
        checks["卡片标题"] = "模块画像与策略建议" in html
        for path, route in (("utils", "l1_unit_pytest"), ("api_handler", "l2_contract_http"),
                            ("db_access", "db_assert"),
                            ("web/index.html", "ui_interaction_idempotency"),
                            ("config.py", "skip")):
            checks[f"{path}→{route}"] = (path in html and route in html)
        checks["理由可见"] = "规则[" in html
        checks["零页面错误"] = not errors
        shot = ROOT / "data" / "reports" / "e_module_card.png"
        page.locator("#rp-overview").screenshot(path=str(shot))
        browser.close()
    ok = all(checks.values())
    for k, v in checks.items():
        print(("✓" if v else "✗"), k)
    print("截图:", shot.name, "| 页面错误:", errors[:2])
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
