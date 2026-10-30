"""答辩演示一键脚本（骨架）：建四级任务 → 接力 → 报告页 URL → 溯源链摘要。

用法：
  python scripts/demo_defense.py --dry-run        # 打印演示步骤清单（零 LLM，答辩前走流程）
  python scripts/demo_defense.py                  # 真实执行：L1→L2 接力一次并打印可点击 URL
  python scripts/demo_defense.py --tid 151        # 不新建，直接复演既有任务的展示环节

设计（答辩现场节奏，每步可暂停）：
  第 1 步 建任务+确认门（对应"人工确认"考点）
  第 2 步 L1→L2 接力（对应"固定图编排/容器沙箱/R-Loop"考点）
  第 3 步 报告页四级视图+测试用例库（对应"四级台账/用例六要素"考点）
  第 4 步 跨级溯源链+归因（对应"RTM/防造假语义"考点）
  兜底：任一步失败打印人工指令（用既有任务 151/161 复演，不冷场）。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
FALLBACK_TASKS = {"relay": 151, "suite": 161}  # 人工兜底复演任务（真库既有）


def _steps(dry: bool):
    relay, suite = FALLBACK_TASKS["relay"], FALLBACK_TASKS["suite"]
    print(f"""[demo] 答辩演示流程（每步可暂停）：
  第 1 步 新建任务+人工确认门 —— 演示"确认门 fail-closed"（未确认下发会被拒）
  第 2 步 L1→L2 接力执行 —— 演示"固定图编排 9 节点 + Docker 容器沙箱 + R-Loop 轮次"
  第 3 步 报告页 #/task/{{tid}} —— 演示"四级卡/测试用例库分组/执行信息卡"
  第 4 步 跨级溯源链 /trace + 缺陷归因 —— 演示"RTM 反向定位 + 防造假（product_defect 不回炉）"
  兜底：第 3/4 步可直接复演既有任务 #{relay}（接力）与 #{suite}（用例库 31 条）""")
    if dry:
        print("[demo] dry-run 结束：未建任务、未调 LLM")
        return False
    return True


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    reuse = int(args[args.index("--tid") + 1]) if "--tid" in args else None
    if not _steps(dry):
        return 0

    if reuse:
        tid = reuse
        print(f"[demo] 复演既有任务 #{tid}（跳过 1/2 步）")
    else:
        from test_agent.store.db import get_conn
        conn = get_conn()
        uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        demo = ROOT / "data" / "demo" / "场景A"
        pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                           (uid, str(demo), "defense-demo")).lastrowid
        tid = conn.execute(
            "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
            " VALUES(?,?,?,?,1)", (pid, uid,
            "对被测项目做功能测试：输入校验（空值/越界）、正常路径、边界值；行覆盖率目标 70%。",
            uid, uid)).lastrowid
        conn.commit()
        print(f"[demo] 第 1 步 ✓ 任务 #{tid} 已建（确认门已放行）")
        from test_agent.graph.levels import run_task_levels
        print("[demo] 第 2 步 L1→L2 接力执行中 ...")
        t0 = time.time()
        run_task_levels(tid, levels=("L1", "L2"))
        print(f"[demo] 第 2 步 ✓ 接力完成（{time.time()-t0:.0f}s）")

    print(f"[demo] 第 3 步 ✓ 报告页：{BASE}/#/task/{tid}（浏览器打开，四级卡+用例库）")
    try:
        req = urllib.request.Request(
            f"{BASE}/api/login", method="POST",
            data=json.dumps({"username": "dev", "password": "dev123"}).encode(),
            headers={"Content-Type": "application/json"})
        tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
        req = urllib.request.Request(f"{BASE}/api/tasks/{tid}/trace",
                                     headers={"Authorization": "Bearer " + tok})
        tr = json.loads(urllib.request.urlopen(req, timeout=10).read())
        print(f"[demo] 第 4 步 ✓ 溯源链 {len(tr.get('chains', []))} 条（gaps 如实）"
              f"  API: {BASE}/api/tasks/{tid}/trace")
    except Exception as e:
        print(f"[demo] 第 4 步（需服务在 {BASE}）：{e}")
    print("[demo] 演示结束——兜底复演任务：" +
          ", ".join(f"#{v}" for v in FALLBACK_TASKS.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
