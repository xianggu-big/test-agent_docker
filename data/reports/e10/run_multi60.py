# -*- coding: utf-8 -*-
"""E10 多语言 60 格驱动器：6 语言 × 5 项目 × 2 采样（串行队列）。

本地项目（test program 十个）+ GitHub 新下载 20 项目混编；排除 JSCore/wordpress
（用户指定）。判定口径同 18 格。每格 JSON 留档；402 不降级。
"""
import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[3]
TP = Path(r"D:\test program")
DEMO = ROOT / "data" / "demo"
OUT = ROOT / "data" / "reports" / "e10" / "multi60_results.json"
TERMINAL = {"done", "failed", "cancelled"}

# (lang, path) × 2 采样 → 60 格
PROJECTS = [
    # java（2 本地 + 2 GitHub + 1 java-api-book 混编？不——按用户口径每语言 5 项目）
    ("java", r"D:\test program\java\spring-petclinic-microservices-main（java_test1）"),
    ("java", r"D:\test program\java\spring-petclinic-main（java_test2）"),
    ("java", r"D:\test program\java\spring-framework-petclinic-main（java_test3）"),
    ("java", TP / "java" / "java-gson"),
    ("java", TP / "java" / "java-json"),
    ("javascript", r"D:\test program\js\fullstack-test-playground-main（js_test1）"),
    ("javascript", r"D:\test program\js\node-rest-api-jest-tests-main（js_test2）"),
    ("javascript", DEMO / "js-calc"),
    ("javascript", TP / "js" / "js-dayjs"),
    ("javascript", TP / "js" / "js-uuid"),
    ("php", r"D:\test program\php\demo-main（php_test1）"),
    ("php", r"D:\test program\php\realworld-laravel-layered-architecture-main（php_test2）"),
    ("php", DEMO / "php-api-book"),
    ("php", DEMO / "php-shop"),
    ("php", TP / "php" / "php-fastroute"),
    ("typescript", r"D:\test program\ts\nestjs-rest-sample-master（ts_test1）"),
    ("typescript", r"D:\test program\ts\application-template-main（ts_test2）"),
    ("typescript", r"D:\test program\ts\nest-app-boilerplate-main（ts_test3）"),
    ("typescript", TP / "ts" / "ts-slugify"),
    ("typescript", TP / "ts" / "ts-ky"),
    ("python", TP / "python" / "python-cachetools"),
    ("python", TP / "python" / "python-schema"),
    ("python", TP / "python" / "python-six"),
    ("python", TP / "python" / "python-slugify"),
    ("python", TP / "python" / "python-humanize"),
    ("go", TP / "go" / "go-uuid"),
    ("go", TP / "go" / "go-cast"),
    ("go", TP / "go" / "go-color"),
    ("go", TP / "go" / "go-env"),
    ("go", TP / "go" / "go-ini"),
]
FALLBACK_REQ = "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"


def main() -> int:
    s = requests.Session()
    tok = s.post(f"{BASE}/api/login", json={"username": "admin", "password": "admin123"},
                 timeout=10).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    # 5 项目 × 2 采样 = 10 格/语言
    cells = []
    for lang, path in PROJECTS:
        for sample in (1, 2):
            cells.append((lang, Path(path), sample))
    print(f"总格数: {len(cells)}", flush=True)

    results = []
    t0, last = time.time(), ""
    done_n = 0
    for i, (lang, path, sample) in enumerate(cells):
        req = FALLBACK_REQ
        rm = path / "REQUIREMENT.md"
        if rm.exists():
            req = rm.read_text(encoding="utf-8", errors="ignore")[:1200]
        tid = s.post(f"{BASE}/api/tasks", headers=h, timeout=10,
                     json={"repo_url": str(path), "requirement": req,
                           "two_stage": False, "ai_reviewer": "off"}).json()["task_id"]
        s.post(f"{BASE}/api/tasks/{tid}/confirm", headers=h, timeout=10)
        s.post(f"{BASE}/api/tasks/{tid}/run", headers=h, timeout=15)
        rec = {"lang": lang, "path": str(path), "sample": sample, "tid": tid,
               "status": "queued", "error": ""}
        results.append(rec)
        print(f"[{i+1}/{len(cells)}] {lang}#{tid} ({path.name[:36]}) 采样{sample} 入队",
              flush=True)
        # 等本格终态（串行队列天然一次一个）
        while True:
            d = s.get(f"{BASE}/api/tasks/{tid}", headers=h, timeout=10).json()
            rec["status"] = d.get("status")
            rec["error"] = (d.get("error") or "")[:160]
            if rec["status"] in TERMINAL:
                break
            time.sleep(20)
        done_n += int(rec["status"] == "done")
        print(f"    → {rec['status']} | {rec['error'][:80]}", flush=True)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n==== 收官: done {done_n}/{len(cells)} ====", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
