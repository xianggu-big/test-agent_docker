"""F（任务卡-369 缺陷修复-20261105）回归验证闭环：缺陷修复后的 fail-to-pass 判定。

通道：阶段二执行图（run_task_exec 吃已批准用例集、不跑 gen 节点）= 现成「执行-only」
载体——验证任务复制原任务最终用例集，gen 不跑=用例天然冻结（修复验证不可能被迁就
用例洗白）。**不用同任务重跑**（A4 重跑 delete-first 会冲掉原始缺陷证据轮次）。

三态判定（fail-to-pass 口径复用归因三闸 Track A 的 SWT-bench 语义）：
- fixed_verified：原缺陷组签名的成员 nodeid 修复后全部 passed
- not_fixed：仍 failed 且签名相同（缺陷原样）
- regressed/new：验证轮出现原缺陷组之外的新签名（如实报新缺陷，不并入旧组）

诚实边界：本地目录项目=用户修完后再点验证（快照即新版，入口提示「请确认已完成修复」）；
远程 git 项目应 checkout 修复 commit 后验证（v2）。依赖 A1 先行（签名基于归因口径）。
"""
from __future__ import annotations

from . import attribution
from .. import config
from .defect_handoff import defect_signature


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
    new_sigs = sorted(s for s in verify_sigs if s not in orig
                      and not (env_members & {
                          r["nodeid"] for r in verify_rows
                          if r["status"] in ("failed", "error")}))
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
