"""黑包阶段3·UI Playwright 真机验收（docs/黑盒能力补齐任务包-20261106.md 阶段3【验收】）。

验收条目逐条：
1. 镜像自检：mta-test:playwright（IMAGE_BUILD_CTX 注册，build_images 构建通过）；
2. demo 前端（data/demo/ui-shop，埋「删除按钮失效」缺陷）→ DOM 快照采集（可达性树=UI 版端点清单）；
3. LLM 按真值清单生成 3 条场景 → 双闸放行 → Playwright 真实执行（截图/trace 落库）；
4. 埋的按钮失效缺陷被场景命中（删除场景 failed）→ 缺陷报告单生成（含截图/trace 证据）；
5. 负例臂：虚构 selector 场景 → 整案剔除且留痕（rejected_reason 可审计）。

护栏：tmp 库 data/tmp_applogs_accept/ui_smoke.db；容器 finally 回收；
不碰真实库、不跑真实任务链。产物：data/reports/ui_assert_accept/smoke_ui_<ts>.json
"""
import io
import json
import shutil
import sys
import tarfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "ui_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "ui_assert_accept"
DEMO = ROOT / "data" / "demo" / "ui-shop"
TS = time.strftime("%Y%m%d-%H%M%S")
SUFFIX = uuid.uuid4().hex[:6]
CONTAINER = f"mta-smoke-ui-{SUFFIX}"
URL = "http://localhost:8080/"


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-ui','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)", (uid, str(DEMO), "ui-shop-demo", None)).lastrowid
    requirement = (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8")
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid
    conn.commit()

    from test_agent.sandbox.runner import SandboxRunner
    runner = SandboxRunner()
    api_key = __import__("os").environ.get("DEEPSEEK_API_KEY", "")
    checks: dict[str, object] = {}
    evidence_root = REPORT_DIR / f"evidence_{TS}"
    started = time.time()
    try:
        # —— 验收条目 1：镜像自检（IMAGE_BUILD_CTX 注册 + 就绪） ——
        checks["镜像注册 IMAGE_BUILD_CTX 且本地就绪"] = (
            config.IMAGE_UI in config.IMAGE_BUILD_CTX and runner.image_exists(config.IMAGE_UI))

        # —— 容器 + demo 服务（单容器 L2 同形态：http.server + playwright 同容器） ——
        container = runner.ensure_container(config.IMAGE_UI, CONTAINER, fresh=True)
        runner.exec_in(container, ["mkdir", "-p", "/work/demo"], timeout_s=10)
        runner.copy_dir_into(container, DEMO, "/work/demo")
        runner.exec_in(container, ["sh", "-c",
                                   "cd /work/demo && nohup python3 -m http.server 8080 "
                                   "> /tmp/http.log 2>&1 & sleep 1"], timeout_s=15)
        probe = runner.exec_in(container, ["python3", "-c",
                                           "import urllib.request;"
                                           "print(urllib.request.urlopen("
                                           "'http://localhost:8080/').status)"],
                               timeout_s=15)
        checks["demo 前端服务就绪（http 200）"] = probe.ok and "200" in probe.stdout

        # —— 验收条目 2：DOM 快照采集（可达性树=UI 版端点清单） ——
        from test_agent.skills.ui_assert import collect_dom_snapshot
        snapshot = collect_dom_snapshot(runner, container, URL)
        checks["快照采集（heading/按钮/输入框进真值清单）"] = (
            any(r["role"] == "heading" for r in snapshot["roles"])
            and any(r["name"] == "添加" for r in snapshot["roles"])
            and "new-todo" in snapshot["ids"]
            and "预置任务-alpha" in " ".join(snapshot.get("texts") or []))
        evidence_root.mkdir(parents=True, exist_ok=True)
        snap_file = evidence_root / "dom_snapshot.json"
        snap_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        # —— 验收条目 3：LLM 按真值清单生成 3 条场景 → 双闸放行 ——
        from test_agent.skills.ui_assert import gen_ui_scenarios
        kept, gen_rejected = gen_ui_scenarios(api_key, snapshot, requirement, base_url=URL)
        checks["LLM 生成 3 条场景全部过闸"] = len(kept) >= 3 and not gen_rejected
        scen_names = [s["name"] for s in kept]

        # —— SkillRegistry 正式通道执行（场景级+步骤级+截图/trace 落库） ——
        from test_agent.skills.builtin import build_skills
        from test_agent.skills.registry import SkillRegistry
        skills = build_skills(runner)
        reg = SkillRegistry(conn=conn)
        for sk in skills.values():
            reg.register(sk)
        res = reg.invoke(tid, skills["ui_assert"], {
            "scenarios": json.dumps(kept, ensure_ascii=False),
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
            "container_name": CONTAINER, "url": URL, "round_no": 1,
            "evidence_dir": str(evidence_root), "requirement": requirement})
        if res.status != "ok":
            raise RuntimeError(f"ui_assert skill 执行失败：{res.error[:300]}")
        out = json.loads(res.output)
        # 证据留档：junit 原文 + 渲染的 pytest 文件（审计可追溯）
        jr = runner.exec_in(container, ["sh", "-c", "cat /tmp/ui_junit_r1.xml 2>/dev/null"],
                            timeout_s=10, workdir="/")
        (evidence_root / "junit_r1.xml").write_text(jr.stdout or "", encoding="utf-8")
        rt = runner.exec_in(container, ["sh", "-c", "cat /work/ui_test_r1.py 2>/dev/null"],
                            timeout_s=10, workdir="/")
        (evidence_root / "ui_test_r1.py").write_text(rt.stdout or "", encoding="utf-8")

        # —— 验收条目 3 续：全部真实执行（截图/trace 落库） ——
        checks["3 条场景真实执行（executed=3）"] = out.get("executed") == 3
        checks["通过/失败分账（≥2 过 + 恰 1 失败）"] = (
            out.get("passed", 0) >= 2 and out.get("failed", 0) == 1)

        # —— 验收条目 4：埋的按钮失效缺陷被命中 + 缺陷报告单 ——
        failed_rows = [r for r in out.get("rows", [])
                       if r.get("step_no") == 0 and r.get("verdict") == "failed"]
        hit_bug = bool(failed_rows) and any(
            "删除" in (s.get("selector") or "") or "删除" in json.dumps(
                [x for x in out.get("rows", []) if x.get("scenario") == failed_rows[0]["scenario"]
                 and x.get("step_no", 0) > 0], ensure_ascii=False)
            for s in failed_rows)
        checks["埋的删除按钮失效缺陷被场景命中"] = bool(hit_bug)
        checks["缺陷报告单生成（md 含失败场景+trace 证据）"] = bool(out.get("defect_report")) and (
            (evidence_root / out["defect_report"]).exists())
        md_text = (evidence_root / out["defect_report"]).read_text(encoding="utf-8") \
            if out.get("defect_report") else ""
        checks["缺陷单含 trace 路径与失败步骤"] = (
            "trace.zip" in md_text and "失败步骤" in md_text and failed_rows
            and failed_rows[0]["scenario"] in md_text)

        # —— 截图/trace 实物核验（落盘文件真实存在） ——
        ev_files = list(evidence_root.rglob("*.png")) + list(evidence_root.rglob("trace.zip"))
        checks["截图/trace 实物落盘（png≥2 + trace.zip≥2）"] = (
            len(list(evidence_root.rglob("*.png"))) >= 2
            and len(list(evidence_root.rglob("trace.zip"))) >= 2)
        checks["证据目录清单"] = [str(p.relative_to(evidence_root)) for p in ev_files][:12]

        # —— 落库核验（ui_asserts 第 30 表：场景级 3 行 + 步骤级行 + round 分档） ——
        n_scene = conn.execute(
            "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=? AND round_no=1 AND step_no=0",
            (tid,)).fetchone()["c"]
        n_step = conn.execute(
            "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=? AND round_no=1 AND step_no>0",
            (tid,)).fetchone()["c"]
        n_shot = conn.execute(
            "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=? AND round_no=1 "
            "AND screenshot_path != ''", (tid,)).fetchone()["c"]
        n_trace = conn.execute(
            "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=? AND round_no=1 "
            "AND trace_path != ''", (tid,)).fetchone()["c"]
        checks["ui_asserts 落库（场景 3 行+步骤行+截图/trace 路径）"] = (
            n_scene == 3 and n_step > 0 and n_shot > 0 and n_trace > 0)

        # —— 验收条目 5（负例臂）：虚构 selector 整案剔除留痕 ——
        neg = [{"name": "虚构元素场景", "steps": [
            {"action": "goto", "url": URL},
            {"action": "click", "selector": "#nonexistent-btn"},
            {"action": "expect_visible", "selector": 'role=button name="幽灵按钮"'},
        ]}]
        res2 = reg.invoke(tid, skills["ui_assert"], {
            "scenarios": json.dumps(neg, ensure_ascii=False),
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
            "container_name": CONTAINER, "url": URL, "round_no": 2,
            "evidence_dir": str(evidence_root), "requirement": requirement})
        out2 = json.loads(res2.output) if res2.status == "ok" else {}
        checks["虚构 selector 整案剔除（rejected=1）"] = out2.get("rejected") == 1 \
            and out2.get("executed", 0) == 0
        n_rej = conn.execute(
            "SELECT COUNT(*) c FROM ui_asserts WHERE task_id=? AND round_no=2 "
            "AND verdict='rejected'", (tid,)).fetchone()["c"]
        rej_reason = conn.execute(
            "SELECT rejected_reason FROM ui_asserts WHERE task_id=? AND round_no=2 "
            "AND verdict='rejected' LIMIT 1", (tid,)).fetchone()
        checks["拒绝留痕可审计（rejected_reason 含虚构 selector）"] = (
            n_rej == 1 and rej_reason is not None
            and ("虚构元素" in (rej_reason["rejected_reason"] or "")
                 or "未命中" in (rej_reason["rejected_reason"] or "")))
        checks["快照落盘（UI 版端点清单）"] = snap_file.exists() and "roles" in json.loads(
            snap_file.read_text(encoding="utf-8"))
        checks["场景名清单（审计）"] = scen_names
    finally:
        try:
            runner.cleanup(CONTAINER)
        except Exception as e:  # noqa: BLE001
            print("清理失败:", CONTAINER, e)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    elapsed = round(time.time() - started, 1)
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid,
              "elapsed_s": elapsed, "container": CONTAINER, "checks": checks,
              "evidence_root": str(evidence_root), "tmp_db": str(TMP_DB)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"smoke_ui_{TS}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in checks.items() if k != "证据目录清单"},
                     ensure_ascii=False, indent=2))
    print(f"[smoke-ui] {elapsed}s | 证据 → {out}")
    ok = all(v for k, v in checks.items() if not k.startswith("场景名清单")
             and k != "证据目录清单")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
