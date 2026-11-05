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

from .defect_handoff import defect_signature


def verify_verdict(orig_defects: list[dict], verify_rows: list[dict],
                   failure_kind: str = "") -> dict:
    """三态判定（纯函数，确定性）。

    orig_defects：原任务缺陷清单（load_defects 产物，含 nodeid/message）；
    verify_rows：验证轮 test_case_results 行（dict：nodeid/status/message）。
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
        elif sig in verify_sigs:
            results.append({"signature": sig, "verdict": "not_fixed",
                            "members": nodeids})
        else:
            results.append({"signature": sig, "verdict": "regressed",
                            "members": nodeids,
                            "note": "成员不再以同签名失败——转 regressed/new 细分见新签名组"})
    new_sigs = sorted(s for s in verify_sigs if s not in orig)
    overall = ("fixed_verified" if new_sigs == [] and
               all(r["verdict"] == "fixed_verified" for r in results) and results
               else "not_fixed" if any(r["verdict"] == "not_fixed" for r in results)
               else "regressed" if new_sigs else "fixed_verified" if results else "no_defects")
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
    cur = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,coverage_target,"
        "status,confirmed,confirmed_by) VALUES (?,?,?,?,?,?,?)",
        (orig["project_id"], orig["user_id"],
         f"[回归验证 of task {tid}] {orig['requirement_text'] or ''}",
         conn.execute("SELECT coverage_target FROM test_tasks WHERE id=?", (tid,)).fetchone()["coverage_target"],
         "ready_for_review", 1, tid))
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
