"""Agent Loop 插件管理器（2026-11-20 设计稿 §4；D2 拍板=目录内固定版本任务内自动安装）。

七步生命周期（宏观稿 §7.2，目录批准=批量预授权合并审批步）：
发现缺口(模型只申请) → 解析目录 → 沙箱安装 → 契约验证 → 任务启用 → 推广/回滚。

安全设计（外部依据：MCP rug pull——Invariant Labs 2025 实锤批准后偷换定义；
本设计的 digest 固定=结构性防御）：
- install 只接受 catalog_id+version，不接受 URL/命令/任意包名；
- digest 固定：封存后每次 fetch 比对，不符=拒绝+告警（内容一变摘要就变=新条目=新审批）；
- 任务容器保持无外网：平台侧预拉取（host pip download）→ sha256 比对 →
  任务容器 --no-index --find-links 离线装；
- local-script 型（如 jacoco 解析器）不安装进容器——平台侧直接执行，digest 封存同样生效；
- plugin_proposals 只申请不安装：Agent 永远不能批准自己（decided_by 恒人工）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

CATALOG_PATH = Path(config.DATA_DIR) / "tool_catalog" / "catalog.json"
CACHE_DIR = Path(config.DATA_DIR) / "tool_cache"
ROOT_SRC = Path(__file__).resolve().parents[2]   # 仓库根（scripts/plugins/ 锚点）
_STATUSES = ("candidate", "verified", "active", "deprecated", "revoked")
# verify 台账 task_id=0 哨兵：契约验证不是任务执行（0=平台级行，报告页按 id 过滤）


class CatalogError(ValueError):
    """目录条目不合规（消息面向管理员）。"""


def load_catalog(path: Path | None = None) -> list[dict]:
    """读目录并校验结构（缺字段/latest/非法 status=拒绝——目录准入规则）。"""
    p = path or CATALOG_PATH
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    tools = doc.get("tools") or []
    for t in tools:
        for field in ("id", "version", "kind", "source", "entrypoint",
                      "permissions", "status"):
            if field not in t:
                raise CatalogError(f"条目缺字段 {field}: {t.get('id')}")
        if t["version"].lower() in ("latest", ""):
            raise CatalogError(f"条目禁用 latest：{t['id']}")
        if t["status"] not in _STATUSES:
            raise CatalogError(f"非法 status：{t['id']}={t['status']}")
    return tools


def find_entry(catalog_id: str, version: str | None = None,
               path: Path | None = None) -> dict | None:
    """按 id（可选版本）取条目——install 的唯一合法入口（不接受 URL/命令）。"""
    for t in load_catalog(path):
        if t["id"] == catalog_id and (version is None or t["version"] == version):
            return t
    return None


def cache_dir_for(entry: dict) -> Path:
    return CACHE_DIR / f"{entry['id']}-{entry['version']}"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal_digest(entry: dict, path: Path | None = None) -> str:
    """首次下载后封存：计算主工件 sha256（管理员人工确认后写回 catalog.json——
    封存是治理动作，不是 Agent 动作）。"""
    d = cache_dir_for(entry)
    if path is None:
        files = sorted(d.glob("*")) if d.exists() else []
        if not files:
            raise CatalogError(f"缓存为空，无法封存：{entry['id']}")
        path = files[0]
    return _sha256_of(path)


def fetch_artifact(entry: dict, timeout_s: int = 300) -> dict:
    """预拉取（平台侧）：host pip download → 缓存目录 → 封存 digest 比对。

    返回 {"ok": bool, "files": [...], "digest": str, "sealed": bool, "detail": str}。
    digest 不符=拒绝（rug pull 结构性防御——内容一变摘要就变=新条目=新审批）。"""
    d = cache_dir_for(entry)
    d.mkdir(parents=True, exist_ok=True)
    src = entry.get("source") or {}
    if src.get("type") in ("local-script",):
        # 平台侧脚本：源文件即工件，直接封存/比对
        src_path = ROOT_SRC / src.get("path", "")
        if not src_path.exists():
            return {"ok": False, "files": [], "digest": "", "sealed": False,
                    "detail": f"源脚本不存在：{src_path}"}
        digest = _sha256_of(src_path)
        declared = entry.get("digest") or ""
        if declared and declared != digest:
            log.error("[tool-catalog] digest 不符（疑似内容被改）：%s", entry["id"])
            return {"ok": False, "files": [str(src_path)], "digest": digest,
                    "sealed": True,
                    "detail": f"digest 不符：声明 {declared[:12]}… 实得 {digest[:12]}…（拒绝）"}
        return {"ok": True, "files": [str(src_path)], "digest": digest,
                "sealed": bool(declared),
                "detail": "seal 新摘要" if not declared else "digest 比对一致"}
    stype = src.get("type") or ""
    if stype in ("php-ext", "go-install"):
        # 非 pip 通道显式拒绝（防错装：pypi 上的同名包与 pecl/go 包毫无关系——
        # pcov/go-install 的实现=容器内 pecl/go install，充值后实施；现在宁可
        # 诚实 unsupported 也不静默错装）
        return {"ok": False, "files": [], "digest": "", "sealed": False,
                "detail": f"{stype} 通道未实现（pecl/go install 容器内安装待实施）——拒绝"}
    pkg = src.get("pkg") or ""
    if not pkg:
        return {"ok": False, "files": [], "digest": "", "sealed": False,
                "detail": "source 缺 pkg"}
    cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "-q",
           "-d", str(d), f"{pkg}=={entry['version']}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "files": [], "digest": "", "sealed": False,
                "detail": f"fetch 超时 {timeout_s}s"}
    files = sorted(f.name for f in d.glob("*") if f.is_file())
    if r.returncode != 0 or not files:
        return {"ok": False, "files": files, "digest": "", "sealed": False,
                "detail": f"pip download 失败 exit={r.returncode}: {r.stderr[-200:]}"}
    digest = _sha256_of(d / files[0])
    declared = entry.get("digest") or ""
    if declared and declared != digest:
        log.error("[tool-catalog] digest 不符（疑似内容被改）：%s", entry["id"])
        return {"ok": False, "files": files, "digest": digest, "sealed": True,
                "detail": f"digest 不符：声明 {declared[:12]}… 实得 {digest[:12]}…（拒绝）"}
    return {"ok": True, "files": files, "digest": digest, "sealed": bool(declared),
            "detail": "seal 新摘要" if not declared else "digest 比对一致"}


def install_in_task(conn, entry: dict, runner, container, task_id: int) -> dict:
    """任务级安装（任务容器保持无外网）：缓存目录拷进容器 /toolcache →
    pip install --no-index --find-links 离线装；local-script 型不装容器
    （平台侧执行，登记 installed 即可）。任何失败 → tool_installations=failed。"""
    def _register(status: str, report: str = "") -> None:
        conn.execute(
            """INSERT INTO tool_installations(task_id, catalog_id, version, status,
                                              installed_at, verify_report)
               VALUES (?,?,?,?,datetime('now'),?)""",
            (task_id, entry["id"], entry["version"], status, report[:2000]))
        conn.commit()

    src = entry.get("source") or {}
    if src.get("type") == "local-script":
        _register("installed", "local-script：平台侧执行，无需容器安装")
        return {"ok": True, "detail": "local-script（无容器安装）"}
    cache = cache_dir_for(entry)
    if not cache.exists() or not any(cache.iterdir()):
        _register("failed", "缓存为空（先 fetch_artifact）")
        return {"ok": False, "detail": "缓存为空（先 fetch_artifact）"}
    try:
        runner.copy_dir_into(container, cache, keep_git=False)
        pkg = src.get("pkg") or entry["id"].split(".")[-1]
        r = runner.exec_in(container,
                           ["pip", "install", "--no-index", "--find-links",
                            "/toolcache", f"{pkg}=={entry['version']}"],
                           timeout_s=300)
        ok = r.exit_code == 0
        _register("installed" if ok else "failed",
                  f"exit={r.exit_code} {((r.stderr or '')[-300:])}")
        return {"ok": ok, "detail": f"pip --no-index exit={r.exit_code}"}
    except Exception as e:      # noqa: BLE001 —— 安装失败如实登记（不炸主链路）
        _register("failed", f"{type(e).__name__}: {str(e)[:300]}")
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


def verify_entry(conn, entry: dict, runner, container) -> dict:
    """契约验证（§7.2 步 5）：三类 fixture 全过 → 目录 status 可升 verified；
    任一败 → 保持 candidate（禁用）。只跑正常路径不算过（设计稿 §4.3）。"""
    fixtures = entry.get("fixtures") or []
    if not fixtures:
        return {"ok": False, "results": [],
                "detail": "条目未声明 fixtures（先补三类契约样例）"}
    results = []
    all_ok = True
    is_local = (entry.get("source") or {}).get("type") == "local-script"
    for fx in fixtures:
        cmd = [c.replace("{entrypoint}", entry["entrypoint"]) for c in fx.get("cmd", [])]
        try:
            if is_local:
                # local-script 型：fixture 在宿主子进程跑（零容器——解析器形态）
                import os
                pr = subprocess.run([sys.executable if c == "python" else c
                                     for c in cmd], capture_output=True, text=True,
                                    timeout=60, check=False, cwd=str(ROOT_SRC),
                                    env={**os.environ, "PYTHONPATH": str(ROOT_SRC)})
                exit_code, out = pr.returncode, pr.stdout or ""
            else:
                r = runner.exec_in(container, cmd, timeout_s=60)
                exit_code, out = r.exit_code, r.stdout or ""
            ok = (exit_code == fx.get("expect_exit", 0)
                  and (fx.get("expect_contains") or "") in out)
        except Exception as e:  # noqa: BLE001
            exit_code, out, ok = None, f"{type(e).__name__}: {e}", False
        results.append({"fixture": fx.get("name"), "ok": ok,
                        "exit": exit_code,
                        "detail": out[:200]})
        all_ok = all_ok and ok
    report = json.dumps(results, ensure_ascii=False)
    if all_ok:
        conn.execute(
            """INSERT INTO tool_installations(task_id, catalog_id, version, status,
                                              installed_at, verify_report)
               VALUES (0, ?, ?, 'verified', datetime('now'), ?)""",
            (entry["id"], entry["version"], report))
    else:
        conn.execute(
            """INSERT INTO tool_installations(task_id, catalog_id, version, status,
                                              installed_at, verify_report)
               VALUES (0, ?, ?, 'failed', datetime('now'), ?)""",
            (entry["id"], entry["version"], report))
    conn.commit()
    return {"ok": all_ok, "results": results,
            "detail": "三类 fixture 全过（可人工升 verified）" if all_ok
            else "存在失败 fixture（保持 candidate）"}


def set_status(conn, catalog_id: str, status: str, by: str = "admin") -> dict:
    """目录状态机（推广/回滚/停用——人工动作）。Agent 永远无权调用。"""
    if status not in _STATUSES:
        return {"ok": False, "detail": f"非法 status：{status}"}
    p = CATALOG_PATH
    doc = json.loads(p.read_text(encoding="utf-8"))
    hit = None
    for t in doc.get("tools") or []:
        if t["id"] == catalog_id:
            t["status"] = status
            t["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            hit = t
            break
    if hit is None:
        return {"ok": False, "detail": f"条目不存在：{catalog_id}"}
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[tool-catalog] %s → %s（by=%s）", catalog_id, status, by)
    return {"ok": True, "detail": f"{catalog_id} → {status}"}


def propose_capability(conn, task_id: int, capability_needed: str,
                       evidence: str) -> int:
    """能力缺口申请（§7.2 步 1）：模型只申请，不安装——Agent 永远不能批准自己。"""
    cur = conn.execute(
        """INSERT INTO plugin_proposals(task_id, capability_needed, evidence, status)
           VALUES (?,?,?,'pending')""",
        (task_id, capability_needed[:500], (evidence or "")[:1000]))
    conn.commit()
    return cur.lastrowid


def decide_proposal(conn, proposal_id: int, approve: bool, by: str) -> dict:
    """人工审批（admin）。Agent 调用面不存在本函数的入口。"""
    conn.execute(
        """UPDATE plugin_proposals SET status=?, decided_by=?, decided_at=datetime('now')
           WHERE id=? AND status='pending'""",
        ("approved" if approve else "rejected", by[:120], proposal_id))
    conn.commit()
    n = conn.execute("SELECT changes() AS n").fetchone()["n"]
    return {"ok": n == 1, "detail": "已审批" if n == 1 else "无 pending 行（可能已审批）"}


def rollback_task_installations(conn, task_id: int) -> int:
    """任务级回滚：任务容器结束即销毁=天然回滚；此处只把台账标 rolled_back
    （目录级回滚走 set_status——deprecated/revoked）。返回行数。"""
    cur = conn.execute(
        """UPDATE tool_installations SET status='rolled_back'
           WHERE task_id=? AND status IN ('installed','verified')""", (task_id,))
    conn.commit()
    return cur.rowcount
