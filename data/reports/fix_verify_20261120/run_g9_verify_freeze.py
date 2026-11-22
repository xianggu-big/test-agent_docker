# -*- coding: utf-8 -*-
"""G9 验证冻结真机验收（2026-11-20 设计文档 §2）。

验证 A2（54e4d62）：验证任务用例集全程只读——指纹前后一致、无新生成文件。
源任务 #1223（corpus-002 本地目录项目，真检出，two_stage=1）。
verdict 合法性：not_fixed（缺陷未修同卷复跑仍在）属正常，判据看的是冻结不是修复。
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent / "g9_verify_freeze.json"
API = "http://127.0.0.1:8000"
SRC_TASK = 1223


def _case_set_snapshot(task_id):
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    rows = c.execute(
        """SELECT sc.filename, sc.content, c.name, c.enabled, c.content
           FROM test_cases c JOIN test_scripts sc ON sc.id=c.script_id
           WHERE sc.task_id=? ORDER BY sc.filename, c.name""", (task_id,)).fetchall()
    c.close()
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return {"n_scripts": len({r[0] for r in rows}), "n_cases": len(rows),
            "fp": hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]}


def _ws_gen_files(task_id):
    ws = ROOT / "data" / "workspaces" / f"task_{task_id}"
    return sorted(p.name for p in ws.glob("test_gen_*")) if ws.exists() else []


def main():
    import httpx
    rec = {"gid": "G9", "steps": [], "pass": False}

    def step(name, ok, detail):
        rec["steps"].append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    tk_r = httpx.post(f"{API}/api/login", json={"username": "fixverify",
                                                "password": "Fv#20261120"}, timeout=10)
    tk = tk_r.json()["token"]

    before = _case_set_snapshot(SRC_TASK)
    r = httpx.post(f"{API}/api/tasks/{SRC_TASK}/verify-fix",
                   headers={"Authorization": f"Bearer {tk}"}, timeout=15)
    vid = r.json().get("verify_task_id") or r.json().get("task_id")
    rec["vid"] = vid
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    vm = c.execute("SELECT verify_mode, status FROM test_tasks WHERE id=?", (vid,)).fetchone()
    step("验证任务创建+冻结标记", vm is not None and vm[0] == 1,
         f"vid={vid} verify_mode={vm[0] if vm else None} status={vm[1] if vm else None}")

    after_create = _case_set_snapshot(vid)
    step("用例集复制原样", after_create["fp"] == before["fp"],
         f"原 {before['n_cases']} 例 fp={before['fp']} / 复制 {after_create['n_cases']} 例 fp={after_create['fp']}")

    # 放行执行（真机容器+真 LLM 执行，无生成调用）
    gen_before = _ws_gen_files(vid)
    httpx.post(f"{API}/api/tasks/{vid}/run", headers={"Authorization": f"Bearer {tk}"}, timeout=10)
    import time
    t0 = time.time()
    tstat = None
    while time.time() - t0 < 1500:
        c2 = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
        tstat = c2.execute("SELECT status FROM test_tasks WHERE id=?", (vid,)).fetchone()[0]
        c2.close()
        if tstat in ("done", "failed", "cancelled"):
            break
        time.sleep(5)
    step("验证轮终态合法", tstat in ("done", "failed"),
         f"status={tstat}（done=修复实证 / failed=not_fixed 或真检出，均为合法终态）")

    msgs = [r[0] for r in sqlite3.connect(
        f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True).execute(
        "SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id=m.session_id "
        "WHERE s.task_id=? ORDER BY m.id", (vid,))]
    freeze = [m for m in msgs if m.startswith("[verify-freeze]")]
    step("冻结指纹留痕两条", len(freeze) == 2, f"条数={len(freeze)}")
    step("指纹前后一致", len(freeze) == 2 and "一致" in freeze[-1],
         f"终态行={freeze[-1][:80] if freeze else '无'}")
    after_run = _case_set_snapshot(vid)
    step("用例集执行后未变", after_run["fp"] == before["fp"],
         f"执行后 fp={after_run['fp']} vs 原 {before['fp']}")
    gen_after = _ws_gen_files(vid)
    step("工作区零新生成", gen_after == gen_before,
         f"test_gen_* 文件 {len(gen_before)}→{len(gen_after)}")
    rec["pass"] = all(s["pass"] for s in rec["steps"])
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nG9 判定: {'PASS' if rec['pass'] else 'FAIL'} → {OUT}", flush=True)
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
