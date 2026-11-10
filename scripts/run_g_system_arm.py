"""G 人机同题对比·系统臂跑批（任务卡-G 材料件 5，2026-11-10）。

同需求（REQUIREMENT.md 逐字）一键跑系统臂，跑完用统一评分管线打分
（scripts/run_g_scoring.py——与人工臂同一把尺）。

用法：
  python scripts/run_g_system_arm.py --profile local   [--n 1] [--no-preflight]
  python scripts/run_g_system_arm.py --profile deepseek [--n 1]

口径：
- 级别=L1（单元级，与人工臂"手写 pytest 单元测试"切片对齐；需求文本含 API
  描述属材料事实，比较前提=按明确需求生成结构化单元测试这一切片，论文注明）；
- 档位切换=临时改 llm_profile 文档 default（profiles 不动），finally 恢复原始
  JSON——净改动为零；跑批期间 server 侧新任务也会跟着走该档位（单机实验窗口
  内可接受，时长=单任务分钟级）；
- 产物=data/corpus/corpus-003/g_system_task<tid>.json（含评分+token 台账+
  墙钟），每跑一份留档、天然幂等可续。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.graph.levels import run_task_levels  # noqa: E402
from test_agent.skills import llm_profile  # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402

CORPUS = ROOT / "data" / "corpus" / "corpus-003"

sys.path.insert(0, str(ROOT / "scripts"))
from run_g_scoring import score_suite  # noqa: E402


def _create_task(conn, requirement: str) -> int:
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, str(CORPUS / "project"), "g-system-arm")).lastrowid
    return conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid


def _swap_default_profile(conn, profile_id: str) -> str:
    """临时把 default 档换成 profile_id，返回原始 value JSON 供恢复。"""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?",
                       (llm_profile.PROFILE_KEY,)).fetchone()
    raw = row["value"] if row else None
    if not raw:
        raise SystemExit("llm_profile 未初始化（先在设置页 apply 过）——如实终止")
    doc = json.loads(raw)
    if profile_id not in (doc.get("profiles") or {}):
        raise SystemExit(f"profile '{profile_id}' 不存在（现有："
                         f"{sorted(doc.get('profiles') or {})}）")
    doc["default"] = profile_id
    doc["routing"] = {}
    conn.execute("UPDATE app_settings SET value=?, updated_by=?, "
                 "updated_at=datetime('now') WHERE key=?",
                 (json.dumps(doc, ensure_ascii=False), "g-system-arm",
                  llm_profile.PROFILE_KEY))
    conn.commit()
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("local", "deepseek"), required=True)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--no-preflight", action="store_true")
    a = ap.parse_args()
    if not a.no_preflight:
        from test_agent.sandbox import preflight
        r = preflight.ensure(auto_repair=True)
        print("[g] 环境：", preflight.summary(r))
        if not r.get("ready"):
            print("[g] 环境未就绪，终止（如实上报，不伪造跑数）")
            return 1
    init_db()
    conn = get_conn()
    requirement = (CORPUS / "project" / "REQUIREMENT.md").read_text(encoding="utf-8")
    raw_saved = _swap_default_profile(conn, a.profile)
    results = []
    try:
        for i in range(a.n):
            t0 = time.monotonic()
            tid = _create_task(conn, requirement)
            conn.commit()
            print(f"[g] run {i + 1}/{a.n} task={tid} profile={a.profile} L1")
            error = None
            try:
                run_task_levels(tid, ("L1",))
            except Exception as e:  # noqa: BLE001 —— 失败留痕不中断后续
                error = f"{type(e).__name__}: {e}"[:300]
                print(f"[g] task {tid} 异常：{error}")
            wall = round(time.monotonic() - t0, 1)
            # token 台账
            tok = conn.execute("SELECT COALESCE(SUM(total_tokens),0) AS t, "
                               "COUNT(*) AS c FROM llm_calls WHERE task_id=?",
                               (tid,)).fetchone()
            # 统一评分（与人工臂同一把尺）：套件=工作区生成用例
            ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
            score = (score_suite(ws, label=f"system_{a.profile}_task{tid}",
                                 do_mutation=True) if ws.exists()
                     else {"error": f"工作区缺失 {ws}"})
            rep = {"task_id": tid, "profile": a.profile, "levels": ["L1"],
                   "wall_clock_s": wall, "llm_calls": tok["c"],
                   "total_tokens": tok["t"], "error": error, "score": score}
            out = CORPUS / f"g_system_task{tid}.json"
            out.write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            print(json.dumps({k: rep[k] for k in
                              ("task_id", "profile", "wall_clock_s", "llm_calls",
                               "total_tokens", "error")}, ensure_ascii=False))
            if "error" not in score:
                print("[g] 评分：", json.dumps(
                    {k: score[k] for k in ("n_tests", "passed", "failed",
                                           "detected_count", "detection_rate",
                                           "wall_clock_s") if k in score},
                    ensure_ascii=False),
                    "| kill_rate:", (score.get("mutation") or {}).get("kill_rate"))
            results.append(rep)
    finally:
        # 恢复原始 profile JSON（净改动为零）
        conn.execute("UPDATE app_settings SET value=?, updated_by=?, "
                     "updated_at=datetime('now') WHERE key=?",
                     (raw_saved, "g-system-arm-restore", llm_profile.PROFILE_KEY))
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
