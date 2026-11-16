"""E9 全量档波次驱动器 v2（任务包 docs/E9规模化普查任务包-20261103.md，多波接力）。

架构：单进程调度器 + 格级 worker 子进程（config 覆盖进程内隔离，N 并发不互染）。
- Arm 1 消融普查：6 主开关全因子 64 组合 × corpus-001/003 × n=3 = 384 格（L1 only）
- Arm 2 四级接力×代审：off/hybrid/auto × corpus-001/002/003 × n=10 = 90 格（L1→L4 全接力）
- L3 需 FastAPI：corpus-003 有数据；corpus-001/002 L3 如实 stop 留档（不算格失败）

护栏（任务包 §4）：
- 全局停车线：llm_calls 累计增量 > 3 亿 → 熔断全队列；
- 402 余额耗尽：BudgetExhausted → 立即停车留痕（zcode 赠送额度口径，余额不管控）；
- 失败率护栏：滑动 20 格失败率 >20% → 告警+并发节流降速；>50% → 停车；
- 每格 JSON 留档本目录：幂等断点续跑（已留档跳过，不重跑不重复计费）；
- 波次锁 .autonomous-loop/e9e10_daily/wave.lock：严禁并行双波。

用法：python run_e9_full.py                # 调度模式
      python run_e9_full.py --worker f.json # 单格 worker（调度器内部调用）
环境变量：E9_CONCURRENCY（默认 4，容器 1GB/个 + DeepSeek 限流自适应）
"""
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
sys.path.insert(0, str(ROOT))

# 模块级导入（2026-11-16 实锤：并行窗口加的 restore_profile_default/
# sweep_stale_running 等函数引用 get_conn/llm_profile 但函数内没导入——
# cron 进程秒退 NameError 三连；模块级一次导入全部函数可用）
from test_agent.skills import llm_profile  # noqa: E402
from test_agent.store.db import get_conn   # noqa: E402

OUT = ROOT / "data" / "reports" / "e9_full"
OUT.mkdir(parents=True, exist_ok=True)
LOCK = ROOT / ".autonomous-loop" / "e9e10_daily" / "wave.lock"
RESULT_MARK = "@@RESULT@@"

# 2026-11-10 口径（docs/开工顺序总编排-20261110.md 拍板）：E9 上限 500 跑、
# 仅 DeepSeek 波谷时段跑、总预算 6500 万 token≈¥450 标准价上限，熔断不追加。
# 本波停车线 = 6500 万 − 实测已花 4,086,127（llm_calls 按留档格 task_id 求和，
# 2026-11-11 00:19 量取；台账 3,796,149 已滞后 29 万，勿再用）。
GLOBAL_LINE = int(os.environ.get("E9_TOKEN_LINE", "60900000"))
RUN_CAP = int(os.environ.get("E9_RUN_CAP", "500"))       # E9 总跑数上限（含已留档全部格）
WINDOW_OPEN_MIN = 0 * 60 + 30      # 波谷窗口开：00:30（北京时间=机器本地时区）
WINDOW_CLOSE_MIN = 8 * 60 + 30     # 波谷窗口关：08:30
CONCURRENCY = int(os.environ.get("E9_CONCURRENCY", "4"))
N_A1 = 3
N_A2 = 10
CORPORA_A1 = ["corpus-001", "corpus-003"]
CORPORA_A2 = ["corpus-001", "corpus-002", "corpus-003"]
MODES_A2 = ["off", "hybrid", "auto"]


def _now_min(now=None) -> float:
    import time as _t
    t = _t.localtime() if now is None else (
        now if hasattr(now, "tm_hour") else _t.localtime(now))
    return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0


# 用户硬规则（2026-11-11 拍板，口径以用户表述为准）：DeepSeek 高峰价时段=
# 09:00–12:00 与 14:00–18:00（比低价贵一倍）——**大批量测试禁止在高峰下发新格**；
# 低价时段=其余全部（00:30–09:00 / 12:00–14:00 / 18:00–次日 00:30）。高峰期做
# 代码/架构改造（零 API 消耗），大批量测试排低价时段。E9_IGNORE_PEAK_GATE=1
# =用户单次明确授权覆盖（2026-11-11 12:02 曾以此在 12:00–14:00 低价窗点火）。
_OFFPEAK_BATCH_SLOTS = [(30, 9 * 60), (12 * 60, 14 * 60), (18 * 60, 24 * 60 + 30)]


def in_offpeak_window(now=None) -> bool:
    """低价批量时段判断（用户硬规则 2026-11-11）：高峰段 09:00–12:00 与
    14:00–18:00 禁止大批量下发（价格贵一倍）；低价段=其余全部。
    E9_IGNORE_PEAK_GATE=1 = 用户单次点火授权：仅放宽段起点（00:30→00:00），
    **高峰段仍硬禁**——覆盖不能授权闯高峰（14:00 起自动停新格，在途跑完）。"""
    m = _now_min(now)
    if 9 * 60 <= m < 12 * 60 or 14 * 60 <= m < 18 * 60:
        return False                           # 高峰段：覆盖也不豁免
    if os.environ.get("E9_IGNORE_PEAK_GATE", "0") == "1":
        return True
    if m >= 24 * 60 - 30:                      # 23:30 后并入次日低价段
        return True
    return any(lo <= m < hi for lo, hi in _OFFPEAK_BATCH_SLOTS)


def minutes_to_window_close(now=None) -> float:
    """当前低价段距收窗的剩余分钟数（三段窗口感知，2026-11-15 修）：
    旧实现只算 00:30–08:30 段——12:00–14:00 与 18:00 后的剩余恒 0，长格门把
    所有格静默延后=整波秒退 plan_exhausted（本日 12:2x 手动点火实锤）。
    语义=距下一个高峰起点：午窗收 14:00；晚窗 18:00 连到次日 09:00（低价段
    18:00–次日 00:30 与 00:30–09:00 相接）；凌晨窗收 09:00。"""
    if os.environ.get("E9_IGNORE_PEAK_GATE", "0") == "1":
        return 9999.0                    # 覆盖模式下长格门同步常开
    m = _now_min(now)
    if 9 * 60 <= m < 12 * 60 or 14 * 60 <= m < 18 * 60:
        return 0.0                       # 高峰段：无可下发窗口（launch 会延后）
    if 12 * 60 <= m < 14 * 60:
        return 14 * 60 - m               # 午窗
    if m >= 18 * 60:
        return (24 * 60 - m) + 9 * 60    # 晚窗连次日 09:00
    if m >= WINDOW_OPEN_MIN:
        return 9 * 60 - m                # 凌晨窗（00:30–09:00）
    return 0.0                           # 00:00–00:30 窗前空隙（窗口未开）

SWITCHES = [  # 6 主开关（V2/V3/V4/V6/V8/V9），combo=开关绝对目标值
    ("GEN_METHODS_ENABLED", True),
    ("PLAN_ENABLED", True),
    ("COVERAGE_FEEDBACK_ENABLED", True),
    ("GEN_BOUNDARY_ATTACK", False),
    ("WITNESS_CONTINUE_ENABLED", False),
    ("GEN_CROSS_REVIEW_ENABLED", False),
]
DEFAULT_STATE = tuple(d for _, d in SWITCHES)


def tokens_total() -> int:
    from test_agent.store.db import get_conn
    c = get_conn()
    n = c.execute("SELECT COALESCE(SUM(total_tokens),0) t FROM llm_calls").fetchone()["t"]
    c.close()
    return int(n)


def tokens_by_task(task_id: int) -> int:
    from test_agent.store.db import get_conn
    c = get_conn()
    row = c.execute("SELECT COALESCE(SUM(total_tokens),0) t FROM llm_calls WHERE task_id=?",
                    (task_id,)).fetchone()
    c.close()
    return int(row["t"])


def build_plan():
    """全量档（2026-11-09 用户拍板：E9 停车线 3 亿 token，五 Arm 推到任务包目标规模）：
    A1=64 组合全因子×2 语料×n=3=384 格；A2=三模式×3 语料×n=10=90 格（全四级接力）；
    A3=2 语料×K=4..30=54 格（K=1..3 由 A1 c00 同构样本覆盖，聚合时并入）；
    A5=corpus-001 r=1..50=50 格（corpus-003 为 FastAPI 依赖语料，宿主重放不适用
    ——诚实边界，留待容器化重放再做）；A4=8 新项目×3 角色×n=5=120 格
    （资产=data/candidates + arm4_projects.json，角色口径沿 run_roles_experiment 先例）。"""
    plan = []

    def _a2():
        for mode in MODES_A2:
            for corpus in CORPORA_A2:
                for n in range(1, N_A2 + 1):
                    plan.append((f"e9a2_{mode}_{corpus}_n{n}", {
                        "arm": "A2", "ai_reviewer": mode, "corpus": corpus, "n": n}))

    def _a3():
        for k in range(4, 31):  # Arm 3：K=4..30 × 2 语料（K=1..3 与 A1 c00 同构不重复生成）
            for corpus in CORPORA_A1:
                plan.append((f"e9a3_{corpus}_k{k:02d}", {
                    "arm": "A3", "corpus": corpus, "k": k,
                    "flags": {key: default for key, default in SWITCHES}}))

    def _a5():
        for r in range(1, 51):  # Arm 5：corpus-001 纯标准库 50 轮（kill_rate 分布 n=50）
            plan.append((f"e9a5_corpus-001_r{r:03d}", {
                "arm": "A5", "corpus": "corpus-001", "round": r}))

    def _a1():
        states = sorted(itertools.product([False, True], repeat=6),
                        key=lambda s: (sum(a != b for a, b in zip(s, DEFAULT_STATE)), s))
        for corpus in CORPORA_A1:
            for idx, state in enumerate(states):
                for n in range(1, N_A1 + 1):
                    plan.append((f"e9a1_{corpus}_{idx:02d}_n{n}", {
                        "arm": "A1", "combo_idx": idx,
                        "flags": {k: bit for (k, _d), bit in zip(SWITCHES, state)},
                        "corpus": corpus, "n": n}))

    def _a4():
        arm4 = json.loads((OUT / "arm4_projects.json").read_text(encoding="utf-8"))
        for proj, meta in arm4["projects"].items():
            for role, rq in meta["roles"].items():
                for n in range(1, arm4.get("n_reps", 5) + 1):
                    cid = f"e9a4_{proj}_{role}_n{n}"
                    plan.append((cid, {
                        "arm": "A4", "project": proj, "role": role, "n": n,
                        "requirement": rq["requirement"], "criteria": rq["criteria"],
                        "source_url": meta["source_url"]}))

    # 2026-11-10 口径的格序（弹药优先+E10 前置）：A3 投票 oracle（E10 Arm4 弹药）→
    # A5 变异杀死分布 → A4 新项目失败语料（E10 Arm1 弹药）→ A2 代审长格 → A1 消融填充。
    # 短格先行也让波谷窗口内的完成数最大化；幂等跳过已留档格。
    _a3()
    _a5()
    _a4()
    _a2()
    _a1()
    return plan


# ==================== worker：单格执行（子进程内 config 隔离） ====================

def _run_two_stage(task_id: int, mode: str) -> dict:
    """两阶段链（hybrid/auto）：阶段一 → 代审钩子（落 review_decisions）→ 脚本代人工放行
    （run_grid 代确认同纪律）→ 阶段二 → L2-L4 手动接力（levels.py 后半段同款记账）。
    auto 走 run_task_auto 同步全链（不经 exec_queue，无双跑竞态；confirmed=1 跳过 D1，
    代审差异集中在 D2 用例集审查循环——聚合报告如实注明）。"""
    from test_agent import config as cfg
    from test_agent.graph.builder import post_prep_review_hook, run_task_auto, run_task_exec, \
        run_task_prep
    from test_agent.graph.l2 import run_l2_task
    from test_agent.graph.l3 import run_l3_task
    from test_agent.graph.l4 import run_l4_task
    from test_agent.store.db import get_conn

    l1 = run_task_prep(task_id)
    if mode == "hybrid":
        post_prep_review_hook(task_id)     # AI 先审一轮（决策落库），停等人工
        l1_exec = run_task_exec(task_id)   # 脚本代人工放行
        l1_final = {**(l1 or {}), **(l1_exec or {})}
    else:  # auto：D2 循环收敛后同步 exec（human 门已满足，D1 跳过）
        ret = run_task_auto(task_id)
        if ret.get("verdict") is not None or ret.get("passed") is not None:
            l1_final = ret                   # 正常完成：返回值即阶段二 final dict
        else:                                # fail-closed：返回 {stage, stopped}
            l1_final = {"auto_stage": ret.get("stage"), "auto_stopped": ret.get("stopped")}
    conn = get_conn()

    def leg(level):
        row = conn.execute("SELECT id FROM test_levels WHERE task_id=? AND level=?",
                           (task_id, level)).fetchone()
        return row["id"] if row else None

    legs = {"L1": l1_final}
    if cfg.LEVELS_L3_ENABLED and cfg.LEVELS_L4_ENABLED:
        l2 = run_l2_task(task_id, trace_from=leg("L1"))
        legs["L2"] = l2
        l3 = run_l3_task(task_id, trace_from=leg("L2"))
        legs["L3"] = l3
        l4 = run_l4_task(task_id, trace_from=leg("L3"))
        legs["L4"] = l4
    return legs


def run_worker(params_path: str) -> int:
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    from test_agent import config
    from test_agent.graph.builder import run_task
    from test_agent.skills import llm_profile
    from test_agent.store.db import get_conn, init_db

    arm = params["arm"]
    if arm == "A4":  # 三角色×新项目：需求=角色文本+验收判据（run_roles_experiment 先例口径）
        corpus = params["project"]
        repo_dir = ROOT / "data" / "candidates" / corpus
        requirement = f"{params['requirement']}\n（验收判据：{params['criteria']}）"
    else:
        corpus = params["corpus"]
        repo_dir = ROOT / "data" / "corpus" / corpus / "project"
        requirement = (repo_dir / "REQUIREMENT.md").read_text(encoding="utf-8")
    init_db()
    conn = get_conn()
    uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
    label = (f"e9a1-c{params['combo_idx']:02d}-{corpus}-n{params['n']}" if arm == "A1"
             else f"e9a2-{params['ai_reviewer']}-{corpus}-n{params['n']}" if arm == "A2"
             else f"e9a3-{corpus}-k{params['k']:02d}" if arm == "A3"
             else f"e9a4-{params['project']}-{params['role']}-n{params['n']}" if arm == "A4"
             else f"e9a5-{corpus}-r{params['round']:03d}")
    ai_mode = params.get("ai_reviewer", "off")
    two_stage = 1 if ai_mode in ("hybrid", "auto") else 0
    pid = conn.execute(
        "INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
        (uid, str(repo_dir), label)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by,"
        "ai_reviewer,two_stage) VALUES(?,?,?,?,1,?,?)",
        (pid, uid, requirement, uid, ai_mode, two_stage)).lastrowid
    conn.commit()

    # 用户指令（2026-11-11）：402 格不许直接掉地——先降级本地备胎重跑，产物打标分账
    # 2026-11-16 降级语义重修（停车实锤：旧实现改全局 app_settings 表 default=local
    # ——4 worker 并发互踩（A 降级 B 的 DeepSeek 格中途变 local）+worker 被杀 finally
    # 不跑=生产 profile 二次污染）。新语义=**格级进程内覆盖**：monkeypatch 本子进程的
    # llm_profile.current 返回 local preset（make_chat/effective_key 全走 current()），
    # 零全局表写、零并发互染、进程退出自动消失。worker=独立子进程（subprocess.run）
    # 是本方案成立的前提。
    profile_override = params.get("profile")
    _orig_current = None
    if profile_override == "local":
        _orig_current = llm_profile.current

        def _local_current(conn=None, _o=_orig_current):
            doc = _o(conn)
            loc = llm_profile.PRESETS.get("local")
            if loc:
                return {**loc, "updated_by": "e9-local-fallback",
                        "updated_at": doc.get("updated_at")}
            return doc

        llm_profile.current = _local_current

    overrides = dict(params.get("flags") or {})
    if arm == "A2":  # 全四级接力：L3/L4 门按任务包口径开启
        overrides["LEVELS_L3_ENABLED"] = True
        overrides["LEVELS_L4_ENABLED"] = True
    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)

    t_before = tokens_total()
    print(f"[worker] {label} task={tid} overrides={overrides}", flush=True)
    rec = {"arm": arm, "corpus": corpus, "n": params.get("n"), "task_id": tid,
           "label": label, "at": time.strftime("%F %T")}
    if arm == "A1":
        rec["combo_idx"] = params["combo_idx"]
        rec["flags"] = params["flags"]
    elif arm == "A2":
        rec["ai_reviewer"] = ai_mode
    elif arm == "A3":
        rec["k"] = params["k"]  # 采样序号（任务级投票的第 k 个独立样本）
    elif arm == "A4":
        rec.update({"project": params["project"], "role": params["role"],
                    "criteria": params["criteria"], "source_url": params["source_url"]})
    else:
        rec["round"] = params["round"]
    try:
        if arm in ("A1", "A3", "A4"):  # A3/A4 与 A1 默认链同构：单阶段 L1（A4 角色需求独立采样）
            final = run_task(tid)
            legs = {"L1": final}
        elif arm == "A2":
            if ai_mode == "off":  # 与 Arm1 同链：单阶段主图 + 四级接力（L3/L4 门已开）
                from test_agent.graph.levels import run_task_levels
                legs = run_task_levels(tid, levels=("L1", "L2", "L3", "L4"))
            else:  # hybrid/auto：两阶段链，D2 代审真实参与（review_decisions 落库）
                legs = _run_two_stage(tid, ai_mode)
        else:  # A5：L1 生成套件 → 变异杀死率仪器（宿主重放，纯标准库语料口径）
            final = run_task(tid)
            legs = {"L1": final}
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "mutation_kill.py"),
                                str(tid), corpus], capture_output=True, text=True,
                               timeout=1800, cwd=str(ROOT))
            mline = next((ln for ln in (r.stdout or "").splitlines()
                          if ln.startswith("{")), None)
            rec["mutation"] = json.loads(mline) if mline else {
                "error": (r.stderr or r.stdout or "")[-300:]}
        rec["legs"] = {lv: {k: legs[lv].get(k) for k in
                            ("verdict", "stop_reason", "passed", "failed", "coverage_line",
                             "round_no") if legs[lv].get(k) is not None}
                       for lv in legs}
        last = legs.get("L1") or {}
        rec.update({"verdict": last.get("verdict"), "stop_reason": last.get("stop_reason"),
                    "passed": last.get("passed"), "failed": last.get("failed"),
                    "status": "done"})
    except Exception as e:  # noqa: BLE001 —— 失败留痕（含 402）
        rec.update({"status": "budget_exhausted" if type(e).__name__ == "BudgetExhausted"
                    else "error", "error": f"{type(e).__name__}: {e}"})
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        if _orig_current is not None:
            llm_profile.current = _orig_current   # 进程内恢复（子进程退出即消失，双保险）
    rec["tokens_spent"] = max(0, tokens_by_task(tid))  # 按 task_id 聚合：并发下 SUM 会互染
    if profile_override:
        rec["profile"] = profile_override               # 打标：聚合分账不混 DeepSeek 口径
    print(RESULT_MARK + json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


# ==================== 调度器 ====================

def acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        print(f"[lock] 已有波次锁：{LOCK.read_text(encoding='utf-8').strip()} → 不启动", flush=True)
        return False
    LOCK.write_text(f"pid={os.getpid()} at={time.strftime('%F %T')} arm=A1+A2", encoding="utf-8")
    return True


def sweep_leftovers():
    for leftover in OUT.glob("*.running"):
        try:
            rec = json.loads(leftover.read_text(encoding="utf-8"))
            rec["status"] = "abandoned_prev_wave"
            (OUT / (leftover.stem + ".json")).write_text(
                json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            leftover.unlink()
            print(f"[resume] 中断格留痕 {leftover.stem}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[resume] 残留 {leftover.name} 处理失败：{e}", flush=True)


def restore_profile_default() -> None:
    """兜底复位 LLM 档位（2026-11-15 实锤：402 降级 worker 被杀时 finally 不跑，
    app_settings 停在 default=local 污染后续所有任务——配置错位比格失败更贵）。
    E9 语义=仅 DeepSeek 波谷跑：default 非 deepseek 一律复位（profiles 保留）。"""
    from test_agent.skills import llm_profile
    from test_agent.store.db import get_conn   # 函数内延迟导入（cron 进程未初始化包路径时同样可用）
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?",
                           (llm_profile.PROFILE_KEY,)).fetchone()
        if not row:
            return
        doc = json.loads(row["value"])
        if doc.get("default") != "deepseek" or doc.get("routing"):
            doc["default"] = "deepseek"
            doc["routing"] = {}
            conn.execute("UPDATE app_settings SET value=?, updated_by='e9-profile-restore',"
                         " updated_at=datetime('now') WHERE key=?",
                         (json.dumps(doc, ensure_ascii=False), llm_profile.PROFILE_KEY))
            conn.commit()
            print("[profile] 检测到遗留非 deepseek 档 → 复位（worker 异退兜底）", flush=True)
    finally:
        conn.close()


def sweep_stale_running(max_age_h: float = 2.0) -> None:
    """陈旧 running 清理（2026-11-15 实锤：worker 被杀时 skill_invocations 悬挂
    running 无终态，台账永远缺这格的结局）：超龄 running 标 error 留痕。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE skill_invocations SET status='failed',"
            " error='worker 异退：调用悬挂无终态（进程被杀），驱动器启动时补记'"
            " WHERE status='running' AND created_at < datetime('now', ?)",
            (f"-{max_age_h} hours",))
        if cur.rowcount:
            conn.commit()
            print(f"[sweep] 陈旧 running 清理 {cur.rowcount} 条 → error 留痕", flush=True)
    finally:
        conn.close()


def main() -> int:
    if "--worker" in sys.argv:
        return run_worker(sys.argv[sys.argv.index("--worker") + 1])
    if not acquire_lock():
        return 2
    try:
        restore_profile_default()
        sweep_stale_running()
        sweep_leftovers()
        baseline = tokens_total()
        t0 = time.time()
        if not in_offpeak_window():
            wait_s = (WINDOW_OPEN_MIN - _now_min()) * 60
            print(f"[gate] 波谷窗口未开，等待 {wait_s / 60:.1f} min 至 00:30 再下发", flush=True)
            time.sleep(max(0.0, min(wait_s, 9 * 3600)))
        print(f"[start] 基线={baseline} 全局停车线=增量>{GLOBAL_LINE:,} "
              f"并发={CONCURRENCY}", flush=True)
        plan = build_plan()

        def _usable(cid: str) -> bool:
            f = OUT / f"{cid}.json"
            if not f.exists():
                return False
            try:
                return json.loads(f.read_text(encoding="utf-8")).get("status") == "done"
            except Exception:  # noqa: BLE001 —— 坏档重跑
                return False

        pending = [(cid, p) for cid, p in plan if not _usable(cid)]
        # 500 跑上限（11-10 口径）：已留档全部格（done/abandoned/error）计入跑数
        archived = sum(1 for cid, _p in plan
                       if (OUT / f"{cid}.json").exists())
        remaining_runs = max(0, RUN_CAP - archived)
        if len(pending) > remaining_runs:
            print(f"[cap] 500 跑上限：已留档={archived} → 本波只取前 {remaining_runs} 格"
                  f"（待跑池 {len(pending)} 截断）", flush=True)
            pending = pending[:remaining_runs]
        max_cells = int(os.environ.get("E9_MAX_CELLS", "0") or 0)
        if max_cells:
            pending = pending[:max_cells]
            print(f"[smoke] E9_MAX_CELLS={max_cells} 小样模式", flush=True)
        print(f"[plan] 总格={len(plan)} 已留档={archived} 本波待跑={len(pending)} "
              f"停车线(增量)={GLOBAL_LINE:,} token 窗口={'内' if in_offpeak_window() else '外'}",
              flush=True)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        stats = {"done": 0, "failed": 0, "tokens": 0}
        params_extra = {}   # cid → {profile: local}（402 降级重跑标记）
        recent: list[bool] = []
        stop_reason = None
        throttle = 0.0

        def launch(cid, params):
            # 波谷门（11-10 口径）：窗口外不下发；长格须给窗口尾留足预算
            if not in_offpeak_window():
                return cid, {"status": "deferred_offpeak", "cid": cid}
            left = minutes_to_window_close()
            need = {"A2": 45, "A5": 20}.get(params["arm"], 10)
            if left < need:
                return cid, {"status": "deferred_offpeak", "cid": cid,
                             "reason": f"窗口余 {left:.0f}min < {need}min（{params['arm']} 长格门）"}
            pfile = OUT / f"{cid}.params"
            pfile.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
            (OUT / f"{cid}.running").write_text(
                json.dumps({**params, "started": time.strftime("%F %T")},
                           ensure_ascii=False), encoding="utf-8")
            timeout = {"A1": 3600, "A2": 7200, "A3": 3600, "A4": 3600, "A5": 7200}[params["arm"]]
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                "--worker", str(pfile)],
                               capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
            result = None
            for line in (r.stdout or "").splitlines():
                if line.startswith(RESULT_MARK):
                    result = json.loads(line[len(RESULT_MARK):])
            if result is None:
                result = {"arm": params["arm"], "corpus": params.get("corpus"),
                          "n": params.get("n"), "status": "error",
                          "error": f"no result (rc={r.returncode}) "
                                   f"stderr={(r.stderr or '')[-300:]}",
                          "tokens_spent": 0, "at": time.strftime("%F %T")}
                for k in ("combo_idx", "flags", "ai_reviewer", "k", "project", "role",
                          "criteria", "source_url", "round"):
                    if k in params:
                        result[k] = params[k]
            return cid, result

        params_by_cid = {cid: p for cid, p in pending}   # 402 降级取参用（2026-11-15 修：
        # 推导式 p 不外泄——降级块 {**p} 在首次真 402 时 NameError，整波崩）
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(launch, cid, p): cid for cid, p in pending}
            for fut in as_completed(futures):
                cid = futures[fut]
                try:
                    cid, rec = fut.result()
                except subprocess.TimeoutExpired:
                    rec = {"status": "error", "error": "worker timeout",
                           "tokens_spent": 0, "at": time.strftime("%F %T")}
                except Exception as e:  # noqa: BLE001
                    rec = {"status": "error", "error": f"scheduler: {type(e).__name__}: {e}",
                           "tokens_spent": 0, "at": time.strftime("%F %T")}
                # 波谷门延后的格：不留档不计数（下个波谷窗口幂等续跑）
                if rec.get("status") == "deferred_offpeak":
                    stats.setdefault("deferred", 0)
                    stats["deferred"] += 1
                    if not in_offpeak_window():
                        stop_reason = "offpeak_window_closed"
                        for f2 in futures:
                            f2.cancel()
                        print(f"[stop] {stop_reason}（已延后 {stats['deferred']} 格）", flush=True)
                        break
                    continue
                # 用户指令（2026-11-11）：402 格先降级本地备胎重跑一次，产物打标分账
                if (rec.get("status") == "budget_exhausted"
                        and os.environ.get("E9_FALLBACK_LOCAL", "1") != "0"
                        and not params_extra.get(cid)):
                    print(f"[fallback] {cid} 402 → 降级 local 档重跑一次", flush=True)
                    params_extra[cid] = {"profile": "local"}
                    pfile2 = OUT / f"{cid}.params"
                    pfile2.write_text(json.dumps({**params_by_cid[cid], **params_extra[cid]},
                                                 ensure_ascii=False), encoding="utf-8")
                    try:
                        cid2, rec2 = launch(cid, {**params_by_cid[cid], **params_extra[cid]})
                    except subprocess.TimeoutExpired:
                        rec2 = {"status": "error", "error": "local fallback timeout",
                                "tokens_spent": 0, "profile": "local_fallback"}
                    rec = rec2
                    cid = cid2
                    restore_profile_default()   # worker 被杀时 finally 不跑 → 主进程兜底
                (OUT / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
                for junk in (OUT / f"{cid}.running", OUT / f"{cid}.params"):
                    junk.unlink(missing_ok=True)
                failed = rec.get("status") != "done"
                stats["done" if not failed else "failed"] += 1
                stats["tokens"] += int(rec.get("tokens_spent") or 0)
                recent.append(failed)
                recent = recent[-20:]
                print(f"[cell] {cid} status={rec.get('status')} "
                      f"spent={rec.get('tokens_spent')} "
                      f"wave={stats['tokens']} elapsed={int(time.time() - t0)}s", flush=True)
                # 用户硬规则（2026-11-11/12）：进高峰段立即停新格——修复漏洞：
                # 旧逻辑只在 launch 时查窗口，预提交长格跨高峰继续跑到 14:28 实锤
                if not in_offpeak_window():
                    stop_reason = "offpeak_window_closed（进高峰停新格，在途跑完）"
                    for f2 in futures:
                        f2.cancel()
                    print(f"[stop] {stop_reason}", flush=True)
                    break
                if rec.get("status") == "budget_exhausted":
                    stop_reason = "budget_exhausted(402)"
                elif tokens_total() - baseline > GLOBAL_LINE:
                    stop_reason = f"global_token_line_{GLOBAL_LINE}（6500万总口径扣实测已花）"
                elif len(recent) == 20 and sum(recent) / 20 > 0.5:
                    stop_reason = "failure_rate>50%"
                elif len(recent) == 20 and sum(recent) / 20 > 0.2:
                    throttle = min(throttle + 30.0, 300.0)
                    print(f"[guard] 滑动失败率>20% → 节流 +{throttle}s/格", flush=True)
                    recent = []
                if stop_reason:
                    for f2 in futures:
                        f2.cancel()
                    print(f"[stop] {stop_reason}", flush=True)
                    break
                if throttle:
                    time.sleep(throttle)
                if (stats["done"] + stats["failed"]) % 5 == 0:
                    print(f"[progress] 完成={stats['done']} 失败={stats['failed']} "
                          f"token={stats['tokens']} ({stats['tokens']/10000:.0f}万) "
                          f"增量={tokens_total() - baseline}", flush=True)
        print(f"[finish] done={stats['done']} failed={stats['failed']} "
              f"tokens={stats['tokens']} 总增量={tokens_total() - baseline} "
              f"elapsed={int(time.time() - t0)}s stop={stop_reason or 'plan_exhausted'}",
              flush=True)
        return 0
    finally:
        release_lock()


def release_lock():
    LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
