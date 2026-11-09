"""运行环境自检与自动补齐（L1 守卫）。

L1 在任务下发前调用：检测 Docker 守护进程、执行镜像、工作区可写、大模型 Key，
缺什么补什么（启动 Docker Desktop → 自动构建缺失镜像），补不了给出可执行的中文提示。

设计要点：
- 只检测 `probe()` 无副作用，界面轮询用；`ensure()` 才真的启动/构建。
- Docker 客户端懒加载且失败不抛异常——环境没准备好是常态，不该让服务崩。
- 依赖可注入（get_client / start_docker 可被测试替换），单测不碰真 Docker。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

DOCKER_WAIT_S = 90          # 等待 Docker Desktop 就绪的上限（秒）
WORKSPACES = None           # None = 用 config.DATA_DIR/"workspaces"（测试可覆写）

_client = None
_client_error = ""


def reset_client() -> None:
    """丢弃缓存客户端，下次调用重新连接（Docker 刚被拉起/测试隔离时用）。"""
    global _client, _client_error
    _client = None
    _client_error = ""


def get_client():
    """取 Docker 客户端；连不上返回 None 并记录原因（不抛）。"""
    global _client, _client_error
    if _client is not None:
        return _client
    try:
        import docker

        client = docker.from_env()
        client.ping()                     # from_env 是懒连接，ping 才真正探活
        _client = client
        _client_error = ""
        return _client
    except Exception as e:                # noqa: BLE001 —— 环境未就绪属于预期分支
        _client = None
        _client_error = f"{type(e).__name__}: {e}"
        return None


def _workspaces_dir() -> Path:
    return WORKSPACES if WORKSPACES is not None else config.DATA_DIR / "workspaces"


def _item(key: str, label: str, status: str, detail: str, blocking: bool = True) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "blocking": blocking}


# ---------- 检测 ----------

def probe() -> dict:
    """只检测、不修复。返回 {ready, items:[{key,label,status,detail,blocking}]}。"""
    items: list[dict] = []
    client = get_client()

    if client is None:
        items.append(_item("docker", "Docker 守护进程", "missing",
                           f"未连接（{_client_error or 'Docker 未启动'}）"))
    else:
        items.append(_item("docker", "Docker 守护进程", "ok", "已连接"))

    for image, ctx in config.IMAGE_BUILD_CTX.items():
        if client is None:
            items.append(_item(f"image:{image}", f"执行镜像 {image}", "missing",
                               "Docker 未就绪，暂无法检测"))
            continue
        try:
            info = client.images.get(image)
            size_mb = round(info.attrs.get("Size", 0) / 1e6)
            items.append(_item(f"image:{image}", f"执行镜像 {image}", "ok", f"已存在（{size_mb} MB）"))
        except Exception:                 # noqa: BLE001 —— ImageNotFound 及驱动差异
            items.append(_item(f"image:{image}", f"执行镜像 {image}", "missing",
                               f"本地缺失，可自动构建（构建上下文 {ctx.name}/）"))

    ws = _workspaces_dir()
    try:
        ws.mkdir(parents=True, exist_ok=True)
        marker = ws / ".write_probe"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
        items.append(_item("workspace", "工作区可写", "ok", str(ws)))
    except Exception as e:                # noqa: BLE001
        items.append(_item("workspace", "工作区可写", "failed", f"{type(e).__name__}: {e}"))

    # 2026-11-09：key 探测走 llm_profile（local 档无需 Key，不得误报"未配置"）
    from ..skills.llm_profile import current as _llm_current, effective_key, missing_key_message
    _llm_p = _llm_current()
    if effective_key():
        _detail = (f"当前模型 {_llm_p.get('provider')}/{_llm_p.get('model')}"
                   + ("（本地 Ollama，无需 Key）" if _llm_p.get("provider") == "local" else ""))
        items.append(_item("llm", "大模型 API Key", "ok", _detail, blocking=False))
    else:
        items.append(_item("llm", "大模型 API Key", "warn",
                           missing_key_message(_llm_p)
                           + "：主链路降级为只跑仓库自带测试（属诚实降级，不阻断）",
                           blocking=False))

    ready = all(i["status"] == "ok" for i in items if i["blocking"])
    return {"ready": ready, "items": items}


# ---------- 自动补齐 ----------

def _docker_desktop_candidates() -> list[Path]:
    if sys.platform != "win32":
        return []
    cands = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "Docker Desktop.exe",
    ]
    return [p for p in cands if p.exists()]


def start_docker(wait_s: int = DOCKER_WAIT_S) -> tuple[bool, str]:
    """尝试启动 Docker Desktop 并等待守护进程就绪。返回 (是否就绪, 说明)。"""
    if get_client() is not None:
        return True, "Docker 守护进程已在运行"

    candidates = _docker_desktop_candidates()
    if not candidates:
        return False, ("未找到可启动的 Docker Desktop：请安装并手动启动 Docker"
                       "（Linux 请启动 dockerd，macOS 请打开 Docker Desktop）")
    try:
        subprocess.Popen([str(candidates[0])],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, f"启动 Docker Desktop 失败：{e}"

    began = time.monotonic()
    while time.monotonic() - began < wait_s:
        reset_client()
        if get_client() is not None:
            return True, f"Docker Desktop 已启动（等待 {int(time.monotonic() - began)}s）"
        time.sleep(3)
    return False, f"Docker Desktop 启动后 {wait_s}s 内仍未就绪，请手动检查"


def ensure(auto_repair: bool = True, wait_s: int = DOCKER_WAIT_S,
           images: list[str] | None = None) -> dict:
    """检测并（可选）自动补齐；返回 probe 结果 + repaired/failed/elapsed_s。

    images=None 时补齐注册表内全部镜像；传入列表则只补指定镜像
    （任务级按需供给：L1「一键准备」只补 Python 执行镜像，Java 镜像链路未接入、仅手动构建）。
    """
    began = time.monotonic()
    repaired: list[str] = []
    failed: list[str] = []

    if not auto_repair:
        snap = probe()
        return {**snap, "repaired": [],
                "failed": [i["key"] for i in snap["items"] if i["blocking"] and i["status"] != "ok"],
                "elapsed_s": 0.0}

    snap = probe()

    # 1) Docker 守护进程
    if any(i["key"] == "docker" and i["status"] != "ok" for i in snap["items"]):
        ok, detail = start_docker(wait_s)
        log.info("环境补齐·Docker: %s（%s）", ok, detail)
        (repaired if ok else failed).append("docker")

    # 2) 执行镜像（Docker 可能刚起来，重新取客户端；只补 images 指定的子集）
    client = get_client()
    wanted = {img: ctx for img, ctx in config.IMAGE_BUILD_CTX.items()
              if images is None or img in images}
    for image, ctx in wanted.items():
        if client is None:
            failed.append(image)
            continue
        try:
            client.images.get(image)
            continue
        except Exception:                 # noqa: BLE001 —— 缺失才构建
            pass
        if not ctx.exists():
            log.error("镜像 %s 的构建上下文不存在: %s", image, ctx)
            failed.append(image)
            continue
        try:
            log.info("自动构建缺失镜像 %s ← %s", image, ctx)
            client.images.build(path=str(ctx), tag=image, rm=True)
            repaired.append(image)
        except Exception as e:            # noqa: BLE001
            log.error("镜像 %s 自动构建失败: %s", image, e)
            failed.append(image)

    # 3) 工作区（probe 里会自己重建，这里只需记录失败）
    ws = _workspaces_dir()
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except Exception:                     # noqa: BLE001
        failed.append("workspace")

    final = probe()
    return {**final, "repaired": repaired, "failed": failed,
            "elapsed_s": round(time.monotonic() - began, 1)}


def summary(result: dict) -> str:
    """把结果压成一句中文（界面/接口提示用）。"""
    if result.get("ready"):
        extra = result.get("repaired") or []
        return "环境就绪" + ("；已自动补齐：" + "、".join(extra) if extra else "")
    missing = [i["label"] for i in result.get("items", []) if i["blocking"] and i["status"] != "ok"]
    return "环境未就绪：" + "；".join(missing or ["未知原因"])
