"""Agent Loop 插件运行时（2026-11-20 设计稿 §4.4）：目录条目 → Capability 动态注册。

准入：仅 status ∈ {verified, active} 且 digest 已封存的条目入菜单（candidate/
deprecated/revoked 即刻不可见——rug pull 防御的第二半：内容固定+状态机双向锁）。
R2 定位（D2 拍板）：目录内固定版本=normal 模式任务内自动，verify/static 拒绝。

原生输出协议（entrypoint 执行返回）：stdout 单行 JSON
  {"status": "ok|error|...", "metrics": {...}, "summary": "...", "code": ...}
→ 归一 CapResult（metrics 走 capabilities._METRIC_KEYS 白名单）；非 JSON 输出
诚实降级 status=ok/failed + summary=stdout 尾部（不伪装结构化）。
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .. import config
from .capabilities import Capability, CapResult, TaskCtx

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("verified", "active")


def load_runtime_entries() -> list[dict]:
    """可入菜单的目录条目（verified/active + digest 已封存）。"""
    from .plugin_manager import CATALOG_PATH, load_catalog
    entries = []
    try:
        for t in load_catalog(CATALOG_PATH):
            if t.get("status") in ACTIVE_STATUSES and t.get("digest"):
                entries.append(t)
    except Exception as e:      # noqa: BLE001 —— 目录损坏=零插件（诚实空态，不炸菜单）
        log.warning("tool_catalog 读取失败（%s: %s）——插件菜单为空", type(e).__name__, e)
    return entries


def _schema_for(entry: dict) -> dict:
    """目录条目 → inputs_schema（通用形态：args 对象透传给 entrypoint）。"""
    return {"type": "object", "properties": {"args": {"type": "object"}},
            "additionalProperties": False}


def _run_local_script(entry: dict, args: dict) -> CapResult:
    """local-script 型：平台侧子进程执行（零容器、零网络——jacoco 解析器形态）。
    超时=timeout 真终止（subprocess timeout + kill）。"""
    src = entry.get("source") or {}
    path = Path(config.DATA_DIR).parent / src.get("path", "")
    if not path.exists():
        path = config.ROOT / src.get("path", "")
    if not path.exists():
        return CapResult(status="failed", summary=f"插件脚本不存在：{src.get('path')}")
    cmd = [sys_exe(), str(path)]
    for k, v in (args.get("args") or {}).items():
        cmd.extend([f"--{k}", str(v)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return CapResult(status="timeout", summary="插件执行超时 60s（真终止）")
    return _parse_output(entry, r.returncode, r.stdout or "", r.stderr or "")


def sys_exe() -> str:
    import sys
    return sys.executable


def _parse_output(entry: dict, exit_code: int, stdout: str, stderr: str) -> CapResult:
    """原生输出 → CapResult 归一（协议外输出诚实降级，不伪装结构化）。"""
    data = None
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except Exception:           # noqa: BLE001
        data = None
    if isinstance(data, dict) and data.get("status"):
        st = data.get("status")
        status = "ok" if st in ("ok", "no_coverage") else "failed"
        summary = str(data.get("message") or data.get("code") or st)[:200]
        return CapResult(status=status, summary=summary,
                         metrics={k: v for k, v in (data.get("metrics") or {}).items()
                                  if isinstance(v, (int, float))},
                         diagnostics=[{"code": str(data.get("code") or st),
                                       "message": summary}],
                         raw_exit=exit_code)
    return CapResult(status="ok" if exit_code == 0 else "failed",
                     summary=(stdout or stderr or "")[-200:],
                     raw_exit=exit_code)


def _run_in_container(entry: dict, args: dict, ctx: TaskCtx) -> CapResult:
    """容器型插件：任务容器内执行 entrypoint（容器无外网——安装已离线完成）。"""
    cmd = entry["entrypoint"].split() + [f"--{k}={v}"
                                         for k, v in (args.get("args") or {}).items()]
    r = ctx.runner.exec_in(ctx.runner.client.containers.get(ctx.container_name),
                           cmd, timeout_s=min(entry_timeout(entry), 600))
    return _parse_output(entry, r.exit_code, r.stdout or "", r.stderr or "")


def entry_timeout(entry: dict) -> int:
    return 300


def catalog_capabilities(ctx: TaskCtx) -> list[Capability]:
    """目录条目 → Capability 列表（动态注册进菜单——能力菜单不是写死的，
    是被测试过程长出来的需求驱动扩展的；R2=normal 模式自动、verify/static 拒）。"""
    caps = []
    for entry in load_runtime_entries():
        langs = entry.get("languages") or ["*"]
        if not ("*" in langs or (ctx.primary_language or "").lower() in langs):
            continue
        is_local = (entry.get("source") or {}).get("type") == "local-script"

        def h(ctx2: TaskCtx, args: dict, _c=entry, _local=is_local) -> CapResult:
            return _run_local_script(_c, args) if _local else _run_in_container(_c, args, ctx2)

        caps.append(Capability(
            id=f"plugin.{entry['id']}",
            version=entry["version"],
            kind="execute",
            languages=langs,
            description=f"[目录插件 {entry['status']}] {entry.get('pain_point') or entry['id']}",
            inputs_schema=_schema_for(entry),
            evidence_kinds=["plugin_out"],
            permissions=entry.get("permissions") or {"fs": [], "network": "none",
                                                     "secrets": []},
            risk="R2",
            timeout_s=entry_timeout(entry),
            handler=h))
    return caps
