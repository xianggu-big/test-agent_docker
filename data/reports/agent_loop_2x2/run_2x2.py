"""AgentLoop 包 4 · 2×2 对照实验驱动器（2026-11-20 设计稿 §6 预注册；D8 拍板=A 臂重跑）。

四臂：A=固定流程+deepseek-chat（旗标关）/ B=自主流程+chat（AGENT_LOOP=1）/
C=固定流程+deepseek-reasoner / D=自主流程+reasoner。项目×n：coupon-api(#1155 需求)/
js-calc(#1156)/go-api-book(#1159) × 3 重复 = 36 格。

纪律：
- 需求文本逐字复用基线 #1155/1156/1159（真库启动时只读提取一次）；
- A/B 唯一变量=AGENT_LOOP（FLAKY_RERUNS 留附加小对照，不混入主 2×2——诚实取舍）；
- 模型切换=tmp 库+monkeypatch config.DEEPSEEK_MODEL（make_chat 兜底链真源）；
- 小样先行：--smoke 先跑 1 格核验全链+成本再放量（批 3 白烧 205k 教训）；
- 停车线：每格后按 token 计价累计（chat 2/8 元每百万、reasoner 4/16——缓存命中
  未计入=估算价），累计 >¥4 立停（预注册 §6.3）；
- 断点续跑：留档文件存在即跳格；
- 每格 JSON 留档（漏斗指标+agent 账本+聚合原料）。

用法：
  python run_2x2.py --smoke            # 1 格（B 臂 js-calc n=1）核验
  python run_2x2.py --arms A           # 只跑 A 臂
  python run_2x2.py --arms A,B         # 主对比两臂
  python run_2x2.py --arms C,D --n 3   # reasoner 两臂
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "data" / "reports" / "agent_loop_2x2"
PARKING_YUAN = 4.0
PRICE = {"deepseek-chat": (2.0, 8.0), "deepseek-reasoner": (4.0, 16.0)}

PROJECTS = [
    {"key": "coupon-api", "lang": "python",
     "repo": str(ROOT / "data" / "demo" / "L2场景-coupon-api"), "baseline_task": 1155},
    {"key": "js-calc", "lang": "javascript",
     "repo": str(ROOT / "data" / "demo" / "js-calc"), "baseline_task": 1156},
    {"key": "go-api-book", "lang": "go",
     "repo": str(ROOT / "data" / "demo" / "go-api-book"), "baseline_task": 1159},
]
ARMS = {
    "A": {"loop": False, "model": "deepseek-chat"},
    "B": {"loop": True, "model": "deepseek-chat"},
    "C": {"loop": False, "model": "deepseek-reasoner"},
    "D": {"loop": True, "model": "deepseek-reasoner"},
}


def load_requirements() -> dict:
    """启动时从真库只读提取基线需求原文（逐字复用纪律——不手抄）。"""
    import sqlite3
    c = sqlite3.connect(ROOT / "data" / "test_agent.db")
    c.row_factory = sqlite3.Row
    out = {}
    for p in PROJECTS:
        r = c.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                      (p["baseline_task"],)).fetchone()
        out[p["key"]] = r["requirement_text"]
    c.close()
    return out


def setup_tmp_db(tmp: Path):
    from test_agent import config
    config.DATA_DIR = tmp
    config.DB_PATH = tmp / "t.db"
    from test_agent.store import db as db_mod
    db_mod.init_db()
    return db_mod.get_conn()


def make_task(conn, project: dict, requirement: str) -> int:
    uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                       "VALUES('driver','x','developer')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                       (uid, project["repo"], project["key"])).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed,"
        "requirement_text) VALUES(?,?,0.7,'ready_for_review',1,?)",
        (pid, uid, requirement)).lastrowid
    conn.commit()
    return tid


def tokens_of(conn, tid: int) -> tuple[int, int, str]:
    """格内 LLM 总 token（prompt/completion）+ 主用模型名。"""
    rows = conn.execute(
        "SELECT model, prompt_tokens, completion_tokens FROM llm_calls WHERE task_id=?",
        (tid,)).fetchall()
    p = sum((r["prompt_tokens"] or 0) for r in rows)
    c = sum((r["completion_tokens"] or 0) for r in rows)
    model = rows[0]["model"] if rows else "-"
    return p, c, model


def agent_ledger(conn, tid: int) -> dict:
    """B/D 臂账本：策略/证据/拒绝（预注册判据测量源）。"""
    import sqlite3
    msgs = [r["content"] for r in conn.execute(
        """SELECT m.content FROM agent_messages m JOIN agent_sessions s
           ON s.id=m.session_id WHERE s.task_id=?""", (tid,))]
    denied = conn.execute(
        """SELECT COUNT(*) AS n FROM skill_invocations si JOIN skills s
           ON s.id=si.skill_id WHERE si.task_id=? AND si.status='denied'""", (tid,)).fetchone()["n"]
    inv = conn.execute(
        """SELECT COUNT(*) AS n FROM skill_invocations si JOIN skills s
           ON s.id=si.skill_id WHERE si.task_id=? AND s.name LIKE 'test.%'""", (tid,)).fetchone()["n"]
    llm_agent = conn.execute(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE task_id=? AND phase LIKE 'agent%'",
        (tid,)).fetchone()["n"]
    strategy = {}
    diag = {}
    for m in msgs:
        if m.startswith("[strategy]") and "策略" in m:
            strategy["msg"] = m[:200]
        if m.startswith("[diagnose]"):
            diag.setdefault("msgs", []).append(m[:160])
    return {"denied": denied, "plugin_invocations": inv,
            "agent_llm_calls": llm_agent, "strategy": strategy, "diagnose": diag,
            "loop_msgs": [m[:160] for m in msgs if m.startswith("[strategy]")
                          or m.startswith("[diagnose]") or m.startswith("[agent-loop")]}


def run_cell(arm: str, project: dict, n: int, requirement: str,
             conn, spent: list[float]) -> dict:
    from test_agent import config
    cfg = ARMS[arm]
    config.AGENT_LOOP_ENABLED = cfg["loop"]
    config.DEEPSEEK_MODEL = cfg["model"]
    if cfg["loop"]:
        config.AGENT_LOOP_MAX_STEPS = 8
        config.AGENT_LOOP_MAX_TOKENS = 30000
        config.AGENT_LOOP_MAX_SECONDS = 600
        config.AGENT_LOOP_DIAG_MAX_ROUNDS = 2
    tid = make_task(conn, project, requirement)
    t0 = time.time()
    error = ""
    try:
        from test_agent.graph.builder import run_task
        final = run_task(tid)
        verdict = final.get("verdict", "-")
        stop_reason = (final.get("stop_reason") or "")[:200]
    except Exception as e:      # noqa: BLE001 —— 格级失败如实留档继续
        verdict, stop_reason = "driver_error", f"{type(e).__name__}: {str(e)[:180]}"
        error = traceback.format_exc()[-800:]
    duration = round(time.time() - t0, 1)
    pt, ct, model = tokens_of(conn, tid)
    price = PRICE.get(model, PRICE["deepseek-chat"])
    yuan = round(pt / 1e6 * price[0] + ct / 1e6 * price[1], 4)
    spent.append(yuan)
    row = conn.execute("SELECT status, error FROM test_tasks WHERE id=?", (tid,)).fetchone()
    rec = {"arm": arm, "project": project["key"], "n": n, "task_id": tid,
           "model": model, "loop": cfg["loop"],
           "status": row["status"], "verdict": verdict,
           "stop_reason": stop_reason, "task_error": (row["error"] or "")[:200],
           "prompt_tokens": pt, "completion_tokens": ct,
           "tokens_total": pt + ct, "cost_yuan_est": yuan,
           "duration_s": duration,
           "agent": agent_ledger(conn, tid) if cfg["loop"] else None,
           "driver_error": error}
    # 格级失败不炸波——停车线是唯一熔断
    return rec


def funnel_of(conn, tid: int) -> dict:
    """TestGen-LLM 漏斗过程指标（调研修正 5）：生成→可构建→通过→覆盖增量。
    MVP 口径：轮次用例数/首轮 vs 终轮通过数/覆盖率行。"""
    rounds = conn.execute(
        """SELECT round_no, passed, failed, skipped, verdict, failure_kind,
                  coverage_line FROM evaluation_rounds
           WHERE task_id=? ORDER BY round_no""", (tid,)).fetchall()
    cov = conn.execute("SELECT coverage_line FROM test_reports WHERE task_id=? "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return {"rounds": [dict(r) for r in rounds],
            "coverage_line": cov["coverage_line"] if cov else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--tag", default="",
                    help="留档文件名后缀（v1 重测=_v1——与 v0 首夜留档分离，"
                         "版本戳纪律：对照必须注明 v0/v1；聚合器 --tag 同步过滤）")
    args = ap.parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    reqs = load_requirements()
    arms = [a.strip().upper() for a in args.arms.split(",")]
    cells = []
    for arm in arms:
        for p in PROJECTS:
            for i in range(1, args.n + 1):
                if args.smoke and not (arm == "B" and p["key"] == "js-calc" and i == 1):
                    continue
                cells.append((arm, p, i))
    spent: list[float] = []
    done, skipped = 0, 0
    print(f"计划 {len(cells)} 格（arms={arms} n={args.n}）；停车线 ¥{PARKING_YUAN}")
    for arm, p, i in cells:
        rec_path = REPORT_DIR / f"cell_{arm}_{p['key']}_{i}{args.tag}.json"
        if rec_path.exists():
            skipped += 1
            old = json.loads(rec_path.read_text(encoding="utf-8"))
            spent.append(old.get("cost_yuan_est", 0))
            print(f"[skip] {arm}/{p['key']}#{i} 已留档")
            continue
        print(f"[run ] {arm}/{p['key']}#{i} …", flush=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"al2x2_{arm}_{i}_"))
        conn = setup_tmp_db(tmp)
        try:
            rec = run_cell(arm, p, i, reqs[p["key"]], conn, spent)
            rec["funnel"] = funnel_of(conn, tid=rec["task_id"])
            rec["requirements_source"] = f"baseline#{p['baseline_task']}"
        except Exception as e:  # noqa: BLE001 —— 保留已跑格记录（除 fatal 仅增补）
            rec = {**(rec if "rec" in dir() else {}),   # run_cell 已产出则保留
                   "driver_fatal": f"{type(e).__name__}: {str(e)[:300]}"}
            rec.setdefault("arm", arm); rec.setdefault("project", p["key"]); rec.setdefault("n", i)
        finally:
            try:
                conn.close()
            except Exception:   # noqa: BLE001
                pass
        rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        done += 1
        total = round(sum(spent), 3)
        print(f"       → {rec.get('verdict', '?')} cost={rec.get('cost_yuan_est')} "
              f"累计=¥{total}/{PARKING_YUAN}（{done} 跑 {skipped} 跳）")
        if total > PARKING_YUAN:
            print(f"[停车线触顶 ¥{total} > ¥{PARKING_YUAN}] 立即停波（预注册 §6.3）——"
                  "已跑格留档有效，聚合用现有格")
            break
    print(f"\n完成：新跑 {done} 跳过 {skipped}；估算总成本 ¥{round(sum(spent), 3)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
