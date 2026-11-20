"""F（任务卡-369 缺陷修复-20261105）回归验证闭环：缺陷修复后的 fail-to-pass 判定。

通道：阶段二执行图（run_task_exec 吃已批准用例集）= 现成「执行-only」载体——验证
任务复制原任务最终用例集。**A2（2026-11-20）修正历史假设**：本文件旧注声称"gen 不跑
=用例天然冻结"——Qoder 体检论断 2 实锤为假（exec 图 evaluate 非 pass/stop → generate
会重新生成/剪除用例）；现在冻结由 verify_mode 列 + 图级路由拦截真实保证（不再靠
"应该不会跑到"的假设），用例集指纹前后留痕 agent_messages = 审计证据。
**不用同任务重跑**（A4 重跑 delete-first 会冲掉原始缺陷证据轮次）。

三态判定（fail-to-pass 口径复用归因三闸 Track A 的 SWT-bench 语义）：
- fixed_verified：原缺陷组签名的成员 nodeid 修复后全部 passed
- not_fixed：仍 failed 且签名相同（缺陷原样）
- regressed/new：验证轮出现原缺陷组之外的新签名（如实报新缺陷，不并入旧组）

诚实边界：本地目录项目=用户修完后再点验证（快照即新版，入口提示「请确认已完成修复」）；
远程 git 项目应 checkout 修复 commit 后验证（v2）。依赖 A1 先行（签名基于归因口径）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import attribution
from .. import config
from .defect_handoff import defect_signature

log = logging.getLogger(__name__)


def case_set_fingerprint(conn, task_id: int) -> str:
    """验证任务用例集指纹（A2 冻结审计）：filename+脚本内容+用例名+enabled+用例内容
    的 sha1 前 16 位。对内容与启用集双向敏感——任何一项被改（生成/剪除/重写/启停）
    指纹必变。只读，不代替写入拦截。"""
    rows = conn.execute(
        """SELECT sc.filename, sc.content, c.name, c.enabled, c.content AS case_content
           FROM test_cases c JOIN test_scripts sc ON sc.id=c.script_id
           WHERE sc.task_id=? ORDER BY sc.filename, c.name""", (task_id,)).fetchall()
    blob = json.dumps(
        [[r["filename"], r["content"], r["name"], r["enabled"], r["case_content"]]
         for r in rows],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def verify_verdict(orig_defects: list[dict], verify_rows: list[dict],
                   failure_kind: str = "", run_diag_text: str = "") -> dict:
    """三态判定（纯函数，确定性）。

    orig_defects：原任务缺陷清单（load_defects 产物，含 nodeid/message）；
    verify_rows：验证轮 test_case_results 行（dict：nodeid/status/message）；
    run_diag_text：验证轮整体执行文本（js_adapter 转换后 stdout，含 stderr 诊断段）——
    408 实锤：per-case message 只有 expect 详情（Expected:0 Received:1），环境类根因
    （ECONNREFUSED）只出现在诊断段，不入参则环境阻塞会被冤判 not_fixed。
    """
    sig_of = lambda d: defect_signature(d, failure_kind)  # noqa: E731
    orig: dict[str, list[str]] = {}      # 签名 → 原缺陷 nodeid 列表
    for d in orig_defects:
        orig.setdefault(sig_of(d), []).append(d["nodeid"])

    verify_status = {r["nodeid"]: r["status"] for r in verify_rows}
    verify_sigs = {sig_of({"message": r.get("message") or "", "nodeid": r["nodeid"]})
                   for r in verify_rows if r["status"] in ("failed", "error")}

    results = []
    for sig, nodeids in orig.items():
        statuses = [verify_status.get(n, "missing") for n in nodeids]
        if all(s == "passed" for s in statuses):
            results.append({"signature": sig, "verdict": "fixed_verified",
                            "members": nodeids})
            continue
        # 仍失败：先看是否环境特征错（ECONNREFUSED 系）——环境阻塞≠缺陷未修（407 实锤：
        # cleandata 组在无 MySQL 容器里注定失败，归 not_fixed 会冤枉修复）
        msgs = [r.get("message") or "" for r in verify_rows
                if r["nodeid"] in nodeids and r["status"] in ("failed", "error")]
        scan_text = "\n".join(msgs + ([run_diag_text] if run_diag_text else []))
        env_hint = next((h for h in attribution._ENV_FAIL_HINTS if h in scan_text),
                        None)
        if env_hint:
            results.append({"signature": sig, "verdict": "env_blocked",
                            "members": nodeids,
                            "note": f"仍失败但为环境特征错（{env_hint}）——补环境后复验"})
        elif sig in verify_sigs:
            results.append({"signature": sig, "verdict": "not_fixed",
                            "members": nodeids})
        else:
            results.append({"signature": sig, "verdict": "regressed",
                            "members": nodeids,
                            "note": "成员不再以同签名失败——转 regressed/new 细分见新签名组"})
    env_members = {n for r in results if r["verdict"] == "env_blocked"
                   for n in r["members"]}
    # 新签名过滤（2026-11-15 闲时找bug 修正）：只剔除"失败行全部属于 env_blocked 组"
    # 的签名（env 噪声换了签名≠新缺陷）。旧写法 not(env_members & 失败行全集) 与被过滤
    # 签名 s 无关——只要存在任一 env_blocked 组就整体吞掉所有新签名 → regressed 假判
    # fixed_verified。逐签名判定，部分属 env 则保留（诚实方向）。
    def _row_sig(r):
        return sig_of({"message": r.get("message") or "", "nodeid": r["nodeid"]})

    new_sigs = sorted(
        s for s in verify_sigs if s not in orig
        and not all(r["nodeid"] in env_members
                    for r in verify_rows
                    if r["status"] in ("failed", "error") and _row_sig(r) == s))
    overall = ("not_fixed" if any(r["verdict"] == "not_fixed" for r in results)
               else "regressed" if new_sigs
               else "fixed_verified" if results and
               all(r["verdict"] in ("fixed_verified", "env_blocked") for r in results)
               and any(r["verdict"] == "fixed_verified" for r in results)
               else "env_blocked" if results and
               all(r["verdict"] == "env_blocked" for r in results)
               else "no_defects")
    return {"overall": overall, "groups": results,
            "new_signatures": new_sigs,
            "new_defect_note": (f"验证轮出现 {len(new_sigs)} 个新失败签名（如实报新缺陷，"
                                 "不并入旧组）") if new_sigs else ""}


# ==================== 0-A 验证资产物化快照（2026-11-20 设计稿 §2.1） ====================
# G9 实锤口径缺口：布考场从 DB 重写用例文件存在 repr 风格差（语义零改但字节不同），
# 整体指纹只能"发现被改"，不能定位文件、不能恢复原样。物化快照三件套：
# 创建时逐文件字节复制 + manifest.json（sha256/行数/AST 用例名单）→ 布考场优先按快照
# 逐字节重写 → 终态分文件对账（style_drift=AST 等价豁免 / substantive_drift=实质改动）。

def snapshot_dir_for(vid: int) -> Path:
    return Path(config.DATA_DIR) / "verify_freeze" / f"task_{vid}"


def _count_lines(data: bytes) -> int:
    """行数口径（materialize 与 reconcile 共用同一函数保证一致）：
    以 \n 计数，末尾无换行的尾行补 1。"""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _py_test_names(text: str) -> list[str] | None:
    """py 脚本的 test_ 函数名单（AST 提取）；语法炸= None（不猜）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    return sorted(n.name for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))


def materialize_verify_snapshot(conn, vid: int) -> dict:
    """创建验证任务时把冻结用例集物化到 data/verify_freeze/task_{vid}/（0-A）。

    每脚本按 DB content 的 utf-8 字节原样落盘 + manifest.json 逐文件 sha256/行数/
    用例名单（py 走 AST）/framework。幂等：manifest 已存在直接返回不重写。
    失败诚实降级：返回 {"ok": False, "reason": ...}（布考场将走 db_fallback 并留痕，
    不炸任务创建主链路）。"""
    d = snapshot_dir_for(vid)
    mf = d / "manifest.json"
    if mf.exists():
        try:
            return {"ok": True, "manifest": json.loads(mf.read_text(encoding="utf-8"))}
        except Exception:      # noqa: BLE001 —— manifest 损坏=重建（下方正常路径覆盖）
            pass
    try:
        d.mkdir(parents=True, exist_ok=True)
        files = []
        for row in conn.execute(
                "SELECT filename, content, framework FROM test_scripts "
                "WHERE task_id=? ORDER BY id", (vid,)):
            fname = row["filename"]
            if not fname:
                continue
            data = (row["content"] or "").encode("utf-8")
            (d / fname).parent.mkdir(parents=True, exist_ok=True)
            (d / fname).write_bytes(data)
            entry = {"filename": fname, "framework": row["framework"],
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "lines": _count_lines(data), "cases": []}
            if fname.endswith(".py"):
                names = _py_test_names(row["content"] or "")
                if names is not None:
                    entry["cases"] = names
            files.append(entry)
        manifest = {"verify_task_id": vid,
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "files": files}
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "manifest": manifest}
    except Exception as e:     # noqa: BLE001 —— 物化失败不炸创建，布考场诚实回退
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def restore_from_snapshot(conn, vid: int, ws: Path) -> dict:
    """布考场恢复（0-A）：verify_mode=1 时优先从冻结快照逐字节重写用例文件。

    返回 {"mode": "snapshot"|"db_fallback", "n_files": int}。快照缺失/manifest 损坏/
    任一文件缺失 → db_fallback（调用方留 [verify-freeze] snapshot_missing 痕）——
    不做半新半旧的部分恢复（冻结语义下混合来源比纯 DB 更难审计）。"""
    mf = snapshot_dir_for(vid) / "manifest.json"
    if not mf.exists():
        return {"mode": "db_fallback", "n_files": 0}
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:          # noqa: BLE001
        return {"mode": "db_fallback", "n_files": 0}
    files = manifest.get("files") or []
    srcs = [(snapshot_dir_for(vid) / f["filename"], f["filename"]) for f in files]
    if not files or any(not p.exists() for p, _ in srcs):
        return {"mode": "db_fallback", "n_files": 0}
    n = 0
    for p, fname in srcs:
        dst = Path(ws) / fname
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(p.read_bytes())
        n += 1
    return {"mode": "snapshot", "n_files": n}


def _ast_dump_of(content: bytes) -> str | None:
    try:
        return ast.dump(ast.parse(content.decode("utf-8", errors="replace")))
    except SyntaxError:
        return None


def _style_drift(got: bytes, expected_path: Path, entry: dict) -> bool:
    """物化风格差判据（G9 形态白名单，0-A）：AST 语义等价（py：ast.dump 相同——
    注释/引号/空白等源码文本差不进 AST）；非 py 退化为行数一致（诚实边界：
    JS 无零依赖语义等价器，行数一致+无 AST 才豁免，宁严勿宽下缺 AST 证据即实质）。"""
    if entry.get("filename", "").endswith(".py"):
        a, b = _ast_dump_of(got), _ast_dump_of(expected_path.read_bytes())
        return a is not None and a == b
    return entry.get("lines") == _count_lines(got)


def reconcile_snapshot(conn, vid: int, ws: Path) -> dict:
    """终态分文件对账（0-A）：workspace 用例文件 vs 冻结快照 manifest 逐文件 sha256。

    漂移三态：style_drift（AST 等价=G9 物化风格差，豁免）/ substantive_drift
    （实质改动：报文件名+行差）/ missing_in_workspace / unexpected_new_file
    （冻结下冒出的 manifest 外新文件）。快照缺失 → missing_snapshot。
    status：match / style_drift（全部漂移均可豁免）/ substantive_drift（含任一实质）。"""
    mf = snapshot_dir_for(vid) / "manifest.json"
    if not mf.exists():
        return {"status": "missing_snapshot", "drift": []}
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:          # noqa: BLE001
        return {"status": "missing_snapshot", "drift": []}
    drift = []
    known = set()
    for entry in manifest.get("files") or []:
        fname = entry["filename"]
        known.add(fname)
        wf = Path(ws) / fname
        if not wf.exists():
            drift.append({"file": fname, "kind": "missing_in_workspace"})
            continue
        got = wf.read_bytes()
        if hashlib.sha256(got).hexdigest() == entry.get("sha256"):
            continue
        if _style_drift(got, snapshot_dir_for(vid) / fname, entry):
            drift.append({"file": fname, "kind": "style_drift"})
            continue
        drift.append({"file": fname, "kind": "substantive_drift",
                      "expected_lines": entry.get("lines"),
                      "actual_lines": _count_lines(got)})
    for p in sorted(Path(ws).glob("test_gen*")):
        if p.name not in known:
            drift.append({"file": p.name, "kind": "unexpected_new_file"})
    status = ("match" if not drift
              else "style_drift" if all(x["kind"] == "style_drift" for x in drift)
              else "substantive_drift")
    return {"status": status, "drift": drift}


def create_verify_task(conn, tid: int) -> int:
    """创建验证任务：同 project、requirement 软链前缀、复制原任务最终用例集
    （test_scripts/test_cases 原样 enabled）、状态直置 ready_for_review+confirmed=1
    （绕过阶段一——run_task_exec 的放行锁以 ready_for_review+confirmed 为准）。
    幂等：同一原任务的未执行验证任务已存在则直接复用（不重复复制）。"""
    orig = conn.execute("SELECT project_id, user_id, requirement_text FROM test_tasks "
                        "WHERE id=?", (tid,)).fetchone()
    if orig is None:
        raise ValueError(f"原任务不存在: {tid}")
    dup = conn.execute(
        "SELECT id FROM test_tasks WHERE project_id=? AND requirement_text LIKE ? "
        "AND status='ready_for_review' ORDER BY id DESC LIMIT 1",
        (orig["project_id"], f"[回归验证 of task {tid}]%")).fetchone()
    if dup is not None:
        return dup["id"]
    have = {r[1] for r in conn.execute("PRAGMA table_info(test_tasks)")}
    sel_cols = ["coverage_target"] + [c for c in ("primary_language", "two_stage") if c in have]
    orig_full = conn.execute(
        f"SELECT {','.join(sel_cols)} FROM test_tasks WHERE id=?", (tid,)).fetchone()
    orig_full = {k: orig_full[k] for k in sel_cols}
    cols = ["project_id", "user_id", "requirement_text", "coverage_target",
            "status", "confirmed", "confirmed_by"]
    vals = [orig["project_id"], orig["user_id"],
            f"[回归验证 of task {tid}] {orig['requirement_text'] or ''}",
            orig_full["coverage_target"], "ready_for_review", 1, tid]
    if "primary_language" in have:                  # 老库无该列（F18 迁移前 schema）时跳过
        cols.insert(4, "primary_language")
        vals.insert(4, orig_full["primary_language"])
    if "two_stage" in have:
        cols.append("two_stage")
        vals.append(1)                              # 验证任务走阶段二执行图
    if "verify_mode" in have:
        cols.append("verify_mode")
        vals.append(1)                              # A2：冻结标记（执行图禁写用例）
    cur = conn.execute(
        f"INSERT INTO test_tasks({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals)
    vid = cur.lastrowid
    for srow in conn.execute("SELECT * FROM test_scripts WHERE task_id=?", (tid,)):
        scur = conn.execute(
            """INSERT INTO test_scripts(task_id, module_id, framework, version, filename,
                                        content, content_hash, confirmed_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (vid, srow["module_id"], srow["framework"], srow["version"], srow["filename"],
             srow["content"], srow["content_hash"], srow["confirmed_by"]))
        for crow in conn.execute(
                "SELECT * FROM test_cases WHERE script_id=? ORDER BY id", (srow["id"],)):
            conn.execute(
                """INSERT INTO test_cases(script_id, source, name, method, content,
                                          enabled, priority, input, steps, expected,
                                          expected_basis)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (scur.lastrowid, crow["source"], crow["name"], crow["method"],
                 crow["content"], crow["enabled"], crow["priority"], crow["input"],
                 crow["steps"], crow["expected"], crow["expected_basis"]))
    conn.commit()
    # 0-A：创建即物化冻结快照（幂等；失败诚实降级——布考场走 db_fallback 并留痕）
    mat = materialize_verify_snapshot(conn, vid)
    if not mat.get("ok"):
        log.warning("verify snapshot 物化失败 vid=%s: %s", vid, mat.get("reason"))
    return vid


def prepare_verify_workspace(conn, vid: int) -> str:
    """验证任务工作区：本地目录项目按 repo_url 快照**当前仓库状态**（=修复后代码，
    407 语义：本地目录快照即新版）；git clone 类项目 v2（应 checkout 修复 commit）。"""
    import shutil
    from pathlib import Path
    from ..sandbox.runner import _load_gitignore_spec
    row = conn.execute(
        """SELECT p.repo_url FROM test_tasks t JOIN projects p ON p.id=t.project_id
           WHERE t.id=?""", (vid,)).fetchone()
    src = Path(row["repo_url"])
    if not src.is_dir():
        raise ValueError(f"verify-fix v1 仅支持本地目录项目，repo_url 不可达: {src}")
    ws = Path(config.DATA_DIR) / "workspaces" / f"task_{vid}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    spec = _load_gitignore_spec(src)
    src_root = src.resolve()
    import fnmatch as _fn
    import os as _os

    def _gi_ignore(directory, names):
        rel_base = Path(directory).resolve().relative_to(src_root)
        skip = []
        for n in names:
            rel = (rel_base / n).as_posix()
            is_dir = (Path(directory) / n).is_dir()
            if any(_fn.fnmatch(n, pat) for pat in config.SKIP_DIRS):
                skip.append(n)
            elif spec is not None and spec.match_file(rel + ("/" if is_dir else "")):
                skip.append(n)
        return skip

    shutil.copytree(src, ws, dirs_exist_ok=True, ignore=_gi_ignore)
    return str(ws)
