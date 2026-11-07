"""镜像管理动态化真机冒烟（Playwright）：分组渲染/徽章/按钮条件/胶囊 N-M 降级。

前置：uvicorn 8021 + 真实库 admin 登录（命令行拿 token 注入 localStorage——
TODO-08 决策维持 localStorage 存储正好是现有口径）。
场景 1-3 用真实 docker 数据（验收 1/2）；场景 4 用 route mock 构造「注册镜像缺失」
（验收 3 的 API 返回模拟，不真删镜像）。
"""
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8021"


def api_login() -> str:
    req = urllib.request.Request(
        BASE + "/api/login", method="POST",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["token"]


def main() -> int:
    tok = api_login()
    results = []

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.add_init_script(f"localStorage.setItem('token', '{tok}')")
        pg.goto(BASE + "/#/settings", wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)

        # 冒烟 1：分组渲染（三组标题存在）
        body = pg.inner_text("#st-images")
        ok_groups = all(g in body for g in ("注册可构建", "本地已存在（未注册）", "辅助依赖"))
        results.append(("分组渲染三组", ok_groups))

        # 冒烟 2：存在/缺失徽章并存（本机 py312 存在 + aux 池必有缺失项如 postgres:16）
        ok_badges = ("存在 " in body) and ("缺失" in body)
        results.append(("徽章存在/缺失并存", ok_badges))

        # 冒烟 3：构建按钮仅出现在「注册可构建且缺失」行——本机三镜像齐备时应为 0 个按钮
        btns = pg.locator("#st-images button", has_text="自动构建").count()
        missing_reg_rows = body.count("缺失")
        results.append(("齐备时无构建按钮", btns == 0))

        # 场景 4：mock 一个注册镜像缺失 → 按钮出现 + 胶囊 N/M（验收 3 的 API 模拟）
        def mock_images(route):
            reg = ["mta-test:py312", "mta-test:node20-jest", "mta-test:jdk17-maven"]
            imgs = []
            for i, n in enumerate(reg):
                imgs.append({"name": n, "source": "registered", "exists": i > 0,
                             "size_mb": None if i == 0 else 500, "buildable": True})
            imgs.append({"name": "mariadb:11", "source": "aux", "exists": True,
                         "size_mb": 458, "buildable": False})
            route.fulfill(json={"images": imgs, "docker_scanned": True})
        pg.route("**/api/images", mock_images)
        pg.evaluate("refreshSettings()")   # 同 hash 路由 goto 不触发重渲染（SPA 事实），显式刷新
        pg.wait_for_timeout(1800)
        body2 = pg.inner_text("#st-images")
        btns2 = pg.locator("#st-images button", has_text="自动构建").count()
        results.append(("缺失注册镜像出现构建按钮", btns2 == 1))
        # 胶囊 N/M：镜像 2/3（探针 env/status 同轮会重取——mock 只拦 /api/images，
        # 探针走 /api/env/status 不受影响，真实探针本机三镜像齐 → 胶囊仍「镜像 ✓」；
        # N/M 降级形态经 renderEnv 纯聚合逻辑验证：此处仅断言胶囊渲染存在
        pills = pg.inner_text("body")
        results.append(("胶囊渲染存在（镜像 ✓ 或 N/M）",
                        ("镜像 ✓" in pills) or ("镜像 " in pills and "/3" in pills)))

        b.close()

    for name, ok in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
