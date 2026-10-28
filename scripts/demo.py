"""一键演示链路：建任务 → 确认门 → 下发执行（R-Loop 全链路）→ 终态摘要。

答辩/复现用。需要 Docker Desktop 与 DEEPSEEK_API_KEY；无 Key 时主链路诚实降级
（跳过 LLM 生成与 plan 拆解，仍可执行仓库自带测试）。

用法：python scripts/demo.py [被测目录] [需求描述]
默认被测对象 = data/demo/场景A
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.builder import run_task
from test_agent.store.db import get_conn


def main() -> int:
    demo_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/demo/场景A")
    requirement = sys.argv[2] if len(sys.argv) > 2 else (
        "对被测项目做功能测试：输入校验（空值/越界）、正常路径、边界值；"
        "行覆盖率目标 70%。")
    if not demo_dir.exists():
        print(f"被测目录不存在：{demo_dir}")
        return 1

    conn = get_conn()
    uname = f"demo_{int(__import__('time').time())}"
    uid = conn.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'developer')",
                       (uname, "x")).lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(demo_dir), f"演示-{demo_dir.name}")).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text) "
                       "VALUES(?,?,?)", (pid, uid, requirement)).lastrowid
    conn.execute("UPDATE test_tasks SET confirmed=1, confirmed_by=? WHERE id=?", (uid, tid))
    conn.commit()
    print(f"[demo] 任务 #{tid} 已建（被测 {demo_dir}），确认门已放行，开始执行 ...")

    final = run_task(tid)
    print("\n===== 终态 =====")
    print(json.dumps({k: final.get(k) for k in
                      ("verdict", "stop_reason", "passed", "failed",
                       "coverage_line", "round_no")}, ensure_ascii=False, indent=2))

    rep = conn.execute("SELECT * FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    if rep:
        print("\n===== 报告摘要 =====")
        if rep["narrative"]:
            print(rep["narrative"][:800])
        else:
            print(f"通过 {rep['passed']} / 失败 {rep['failed']} / 覆盖率 {rep['coverage_line']}")
    rtm = conn.execute("SELECT matrix FROM rtm_maps WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    if rtm:
        m = json.loads(rtm["matrix"])
        cov = sum(1 for x in m if x["status"] == "covered")
        print(f"\n===== RTM ===== 条件覆盖 {cov}/{len(m)}"
              + ("；缺口：" + "; ".join(x["condition"][:30] for x in m if x["status"] == "gap")[:200]
                 if cov < len(m) else ""))
    print(f"\n[demo] 详情：python cli.py report {tid} ｜ python cli.py log {tid}"
          f" ｜ 浏览器报告页任务 #{tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
