# -*- coding: utf-8 -*-
"""驱动器公共层 v2（2026-11-19 遗憾清偿件 5）：两次实锤事故的病根修复。

事故一（2026-11-18 bugverify 首拍）：run 不检查响应码——env_gate 409（Docker 未就绪）
后六格停在"任务 queued+无队列行"的假排队态，驱动器照常轮询永远等不到终态。
事故二（同日）：定时进程死亡无任何报错（会话重启连带杀后台进程）——静默失败。

本层三条纪律：
1. 点火前自检（server 活+Docker 活）——死了当场报错，绝不静默等待；
2. 每步校验响应码——409 按 detail 分流（env_gate 等待重试 / 已排队如实跳过 /
   未知 409 上抛），绝不把失败当成功；
3. 纯函数化 409 分流——单测可锁（tests/test_driver_kit.py）。
"""
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]


def decide_409(detail: str) -> str:
    """409 detail 分流（纯函数）：返回 'env_gate' / 'already_queued' / 'fatal'。

    依据=server.py 各 409 措辞（硬编码对齐，措辞改动由单测锁住）：
    - "运行环境未就绪" → env_gate（Docker/工作区自动补齐中，等待重试）；
    - "正在队列中等待执行" / "任务正在执行中" → already_queued（幂等，不重试）；
    - 其余（未确认/代审拒绝/两阶段门）→ fatal（配置错误，重试无意义）。
    """
    d = detail or ""
    if "运行环境未就绪" in d:
        return "env_gate"
    if "正在队列中等待执行" in d or "任务正在执行中" in d:
        return "already_queued"
    return "fatal"


def ensure_server(base: str = "http://127.0.0.1:8000", timeout_s: int = 30) -> str:
    """点火前自检：server 活（登录探针）——死了抛 RuntimeError 当场报错。
    返回 admin token。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = requests.post(f"{base}/api/login",
                              json={"username": "admin", "password": "admin123"}, timeout=5)
            if r.status_code == 200:
                return r.json()["token"]
        except requests.ConnectionError:
            pass
        time.sleep(2)
    raise RuntimeError(f"server {base} 不可达（{timeout_s}s 内登录探针始终失败）"
                       "——点火前自检失败，拒绝静默等待（驱动器 v2 纪律）")


def ensure_docker(timeout_s: int = 60) -> None:
    """点火前自检：Docker daemon 活（env_gate 409 的主诱因）——不可达先拉起等待。"""
    import subprocess
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    raise RuntimeError(f"Docker daemon {timeout_s}s 内不可用——先修环境再点火"
                       "（env_gate 409 六格假排队事故的预防）")


def create_and_run(s: "requests.Session", h: dict, base: str, cell: dict,
                   max_env_gate_retry: int = 3, retry_wait_s: int = 20) -> dict:
    """创建+确认+下发，每步校验响应码（事故一的修复）。

    cell: {"path","requirement","two_stage","ai_reviewer"}；返回含 tid/steps 审计。
    run 遇 env_gate 409 → 等待重试 ≤max_env_gate_retry（Docker 自动补齐中）；
    already_queued → 如实记录（幂等）；fatal → RuntimeError 上抛（配置错误）。
    """
    tid = s.post(f"{base}/api/tasks", headers=h, timeout=10, json={
        "repo_url": cell["path"], "requirement": cell["requirement"],
        "two_stage": cell.get("two_stage", False),
        "ai_reviewer": cell.get("ai_reviewer", "off")}).json()["task_id"]
    steps = []
    for path in ("confirm", "run"):
        for attempt in range(max_env_gate_retry + 1):
            r = s.post(f"{base}/api/tasks/{tid}/{path}", headers=h, timeout=15)
            if r.status_code == 200:
                steps.append({"step": path, "http": 200})
                break
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:                     # noqa: BLE001 —— 非 JSON 响应体
                detail = r.text[:120]
            verdict = decide_409(detail) if r.status_code == 409 else "non_409"
            steps.append({"step": path, "http": r.status_code, "detail": detail[:100],
                          "verdict": verdict, "attempt": attempt})
            if verdict == "already_queued":
                break                              # 幂等：已排队不是失败
            if verdict == "env_gate" and attempt < max_env_gate_retry:
                time.sleep(retry_wait_s)           # Docker 自动补齐中——等待重试
                continue
            raise RuntimeError(f"任务 {tid} {path} 失败 http={r.status_code} "
                               f"verdict={verdict} detail={detail[:120]}")
    return {"tid": tid, "steps": steps}


if __name__ == "__main__":
    token = ensure_server()
    ensure_docker()
    print("[driver_kit] 自检通过：server 活 + Docker 活")
