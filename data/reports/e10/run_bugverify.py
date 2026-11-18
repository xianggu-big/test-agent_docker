# -*- coding: utf-8 -*-
"""因平台 bug 失败任务的重跑验证批（2026-11-18 用户拍板：加入波谷队列）。

选格=被平台 bug 直接打断、修复后未真机验证的任务（每格带预期修复族）。
时间：18:01 低价窗点火，14:00 硬停护栏沿用。判定口径=18 格验收标准。
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "reports" / "e10" / "bugverify_results.json"
TERMINAL = {"done", "failed", "cancelled"}

# 修复族 → 待验证项目（每族 1 采样，格式路径均按 test program 实际位置）
CELLS = [
    {"family": "族D ts-jest 嵌套依赖（第三层 diagnostics）",
     "path": r"D:\test program\ts\nestjs-rest-sample-master（ts_test1）",
     "requirement": "测试所有 REST 接口的核心功能：正常输入、边界值、非法输入与错误处理。"},
    {"family": "族D ts lib 落后连坐",
     "path": r"D:\test agent_docker\data\demo\ts-calc",
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"family": "族D ts-jest+testMatch 双修",
     "path": r"D:\test program\ts\application-template-main（ts_test2）",
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"family": "族C gson JPMS 多模块（部分修复待验证）",
     "path": r"D:\test agent_docker\data\demo\go-shop",
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"family": "族A java 多模块 jacoco（microservices）",
     "path": r"D:\test program\java\spring-petclinic-microservices-main（java_test1）",
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
    {"family": "族E laravel 新形态熔断（待取证）",
     "path": r"D:\test program\php\realworld-laravel-layered-architecture-main（php_test2）",
     "requirement": "测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。"},
]


def wait_offpeak() -> None:
    """等到 18:01 低价窗（硬规则：高峰段不跑）。"""
    now = time.time()
    target = time.strptime(time.strftime("%Y-%m-%d") + " 18:01", "%Y-%m-%d %H:%M")
    wait = max(0, time.mktime(target) - now)
    if wait > 0:
        print(f"等待 {wait/3600:.1f}h 至 18:01 低价窗", flush=True)
        time.sleep(wait)


def main() -> int:
    wait_offpeak()
    s = requests.Session()
    h = {"Authorization": "Bearer " + s.post(f"{BASE}/api/login",
         json={"username": "admin", "password": "admin123"}, timeout=10).json()["token"]}
    results = []
    for i, cell in enumerate(CELLS):
        tid = s.post(f"{BASE}/api/tasks", headers=h, timeout=10,
                     json={"repo_url": cell["path"], "requirement": cell["requirement"],
                           "two_stage": False, "ai_reviewer": "off"}).json()["task_id"]
        s.post(f"{BASE}/api/tasks/{tid}/confirm", headers=h, timeout=10)
        r = s.post(f"{BASE}/api/tasks/{tid}/run", headers=h, timeout=15)
        rec = {"family": cell["family"], "path": cell["path"], "tid": tid,
               "run_http": r.status_code, "status": "queued", "error": ""}
        results.append(rec)
        print(f"[{i+1}/{len(CELLS)}] {cell['family'][:22]} → #{tid} 入队", flush=True)
    # 串行等全批终态
    t0, last = time.time(), ""
    while time.time() - t0 < 6 * 3600:
        states = []
        for rec in results:
            d = s.get(f"{BASE}/api/tasks/{rec['tid']}", headers=h, timeout=10).json()
            rec["status"] = d.get("status")
            rec["error"] = (d.get("error") or "")[:160]
            states.append(f"#{rec['tid']}:{rec['status']}")
        line = " ".join(states)
        if line != last:
            print(f"[{int((time.time()-t0)/60):>2}m] {line}", flush=True)
            last = line
        if all(r["status"] in TERMINAL for r in results):
            break
        time.sleep(30)
    # 14:00/18:00 硬停也收口
    print("\n==== 重跑验证批终态 ====", flush=True)
    ok = 0
    for rec in results:
        env_ok = (rec["status"] == "done" or "疑似产品缺陷" in rec.get("error", "")
                  or "覆盖率" in rec.get("error", "") or "工具链" in rec.get("error", ""))
        ok += int(env_ok)
        print(f"{rec['family'][:24]} #{rec['tid']}: {rec['status']} "
              f"[{'✅ 修复实证' if env_ok else '❌ 仍失败'}] {rec['error'][:90]}", flush=True)
    print(f"\n修复实证 {ok}/{len(results)}", flush=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
