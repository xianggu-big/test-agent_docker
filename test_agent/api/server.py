"""L1 应用层：FastAPI REST + 三角色守卫 + 静态界面（阶段 2，按图纸排版）。

约定：
- 鉴权：Bearer token（auth.py 内存 token，重启失效）；角色 admin 全部 / developer 除设置管理 / caller 仅新建·列表·报告
- 下发执行：后台线程跑 run_task，前端轮询 /api/tasks/{tid}；builder 的 A4 乐观锁兜底并发
- 人工插用例：落库 source='imported' + 重建工作区 test_imported.py（下次 run 随快照进容器）；
  name 经 A5 规整保证与 nodeid 一致
- 运行中任务禁止编辑/确认变更；停止仅对未下发任务生效（运行中不可强停，容器随任务回收）

启动：python -m uvicorn test_agent.api.server:app --port 8000  （禁用 --reload 跑真任务）
"""
import base64
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from .. import config
from ..graph.builder import run_task
from ..sandbox import preflight
from ..sandbox.runner import SandboxRunner
from ..skills.ai_reviewer import spotcheck_sample
from ..skills.defect_handoff import build_handoff
from ..skills.evaluate import hotspot_coverage
from ..skills.gen_tests import _rebuild_imported_file, _sanitize_name
from ..skills.traceability import build_task_traces, build_trace_chain
from ..store import db
from ..store.db import get_conn
from . import auth

log = logging.getLogger(__name__)

ACTIVE = {"cloning", "analyzing", "provisioning", "generating", "running", "reporting"}
PROGRESS = {"queued": 5, "cloning": 10, "analyzing": 25, "provisioning": 40,
            "generating": 55, "ready_for_review": 60, "running": 70, "reporting": 85,
            "done": 100, "failed": 100, "cancelled": 100}
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="容器化大模型软件测试 Agent", version="1.0.0")
_runner: SandboxRunner | None = None


def get_runner() -> SandboxRunner:
    global _runner
    if _runner is None:
        _runner = SandboxRunner()
    return _runner


# ---------- 运行环境自检 / 自动补齐（L1 守卫，见 sandbox/preflight.py） ----------
_ENV_LOCK = threading.Lock()
_ENV_STATE: dict = {"state": "idle", "message": "尚未检测", "result": None,
                    "started_at": None, "finished_at": None}


def _env_worker() -> None:
    try:
        # L1 一键准备补齐 Python + JS 两个执行镜像（Java 链路 v1 未接入，其镜像仅设置页手动构建）——
        # 任务级按需供给：真正缺什么由 provision 在 analyze 测出语言后决定
        result = preflight.ensure(auto_repair=True, images=[config.IMAGE_PY, config.IMAGE_JS])
    except Exception as e:  # noqa: BLE001 —— 自检自身失败也不能炸服务
        log.error("环境自检异常: %s: %s", type(e).__name__, e)
        result = {"ready": False, "items": [], "repaired": [], "failed": ["internal"],
                  "elapsed_s": 0.0}
    with _ENV_LOCK:
        _ENV_STATE.update({
            "state": "ready" if result.get("ready") else "failed",
            "message": preflight.summary(result),
            "result": result,
            "finished_at": time.time(),
        })


def trigger_env_prepare() -> dict:
    """触发一次异步环境补齐；已在跑则直接返回当前状态。"""
    with _ENV_LOCK:
        if _ENV_STATE["state"] == "running":
            return dict(_ENV_STATE)
        _ENV_STATE.update({"state": "running", "message": "正在检测并自动补齐运行环境…",
                           "result": None, "started_at": time.time(), "finished_at": None})
    threading.Thread(target=_env_worker, daemon=True).start()
    return dict(_ENV_STATE)


def env_gate() -> None:
    """L1 环境守卫：Docker/工作区不就绪则触发自动补齐并拒绝下发（409）。

    镜像不在此拦截——主语言要到图内 analyze 才测出，缺镜像由 provision
    按沙箱自适应模式分流（自动构建 / 诚实 stop），这里拦反而会误伤。
    """
    env = preflight.probe()
    blocking = [i for i in env["items"]
                if i["blocking"] and i["status"] != "ok" and not i["key"].startswith("image:")]
    if not blocking:
        return
    trigger_env_prepare()
    missing = "、".join(i["label"] for i in blocking)
    raise HTTPException(
        409, f"运行环境未就绪（{missing}），已自动开始补齐；"
             f"进度见环境状态栏，完成后可再次下发")


# ---------- 鉴权依赖 ----------

def current_user(request: Request) -> dict:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = auth.TOKENS.get(token)
    if not user:
        raise HTTPException(401, "未登录或登录已失效（服务可能已重启）")
    return user


def require_roles(*roles: str):
    """依赖工厂：返回可直接放进 Depends 的 callable（不能预包 Depends，会双重包装）。"""
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, f"角色 {user['role']} 无权执行此操作")
        return user
    return dep


admin = require_roles("admin")
staff = require_roles("admin", "developer")


def _task_or_404(conn, tid: int):
    row = conn.execute(
        """SELECT t.*, p.name AS project_name, p.repo_url FROM test_tasks t
           JOIN projects p ON p.id = t.project_id WHERE t.id=?""", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"任务 {tid} 不存在")
    return row


# ---------- 认证与用户 ----------

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.post("/api/login")
def api_login(body: dict, request: Request):
    ip = _client_ip(request)
    if not auth.rate_limit(f"login:{ip}", 20, 300):
        raise HTTPException(429, "尝试过于频繁，请 5 分钟后再试")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        raise HTTPException(422, "用户名与密码必填")
    result = auth.login(username, password)
    if result is None:
        auth.record_event(username, "failed")          # 失败尝试也记（使用记录完整性）
        raise HTTPException(401, "用户名或密码错误")
    auth.record_event(username, "login", result["user_id"])
    return result


@app.post("/api/logout")
def api_logout(request: Request, user: dict = Depends(current_user)):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    auth.TOKENS.pop(token, None)
    auth.record_event(user["username"], "logout", user["user_id"])
    return {"ok": True}


@app.post("/api/register")
def api_register(body: dict, request: Request):
    """开放注册（任务 4）：默认 caller 角色。每 IP 每小时 10 次（防刷库）。"""
    ip = _client_ip(request)
    if not auth.rate_limit(f"register:{ip}", 10, 3600):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    result = auth.register(str(body.get("username", "")).strip(),
                           str(body.get("password", "")))
    if result is None:
        raise HTTPException(422, "注册失败：用户名需 3-24 位字母/数字/下划线且未被占用，口令至少 6 位")
    return {"ok": True, "username": result["username"]}


@app.get("/api/me")
def api_me(user: dict = Depends(current_user)):
    return user


@app.get("/api/me/usage")
def api_my_usage(user: dict = Depends(current_user)):
    """个人信息页（任务 4）：我的测试任务（建立/开始/状态）+ 我的登录记录。"""
    conn = get_conn()
    uid = user["user_id"]
    tasks = [dict(r) for r in conn.execute(
        """SELECT t.id, t.status, t.created_at, t.updated_at,
                  p.name AS project_name, t.requirement_text,
                  (SELECT MIN(m.created_at) FROM agent_messages m
                    JOIN agent_sessions s ON s.id = m.session_id
                    WHERE s.task_id = t.id) AS first_run_at
           FROM test_tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.user_id=? ORDER BY t.id DESC LIMIT 100""", (uid,))]
    events = [dict(r) for r in conn.execute(
        """SELECT event, created_at FROM login_events
           WHERE user_id=? ORDER BY id DESC LIMIT 50""", (uid,))]
    return {"tasks": tasks, "events": events,
            "registered_at": conn.execute("SELECT created_at FROM users WHERE id=?",
                                          (uid,)).fetchone()["created_at"]}


@app.get("/api/users")
def api_users(user: dict = Depends(admin)):
    rows = get_conn().execute("SELECT id, username, role, created_at FROM users").fetchall()
    return [dict(r) for r in rows]


# ---------- 任务主链路（新建→确认→下发→列表→报告→日志） ----------

@app.post("/api/tasks")
def api_create_task(body: dict, user: dict = Depends(current_user)):  # 调用者也可新建测试（按图纸权限表）
    repo_url = str(body.get("repo_url", "")).strip()
    requirement = str(body.get("requirement", "")).strip()
    if not repo_url or not requirement:
        raise HTTPException(422, "项目地址与自然语言需求必填")
    auto_prepare = 1 if body.get("auto_prepare") else 0
    two_stage = 1 if body.get("two_stage") else 0
    ai_reviewer = str(body.get("ai_reviewer") or "off").strip()
    if ai_reviewer not in ("off", "hybrid", "auto"):
        raise HTTPException(422, "ai_reviewer 必须是 off/hybrid/auto")
    from ..store.db import write_conn
    # B2 项目归一：同一仓库/路径复用同一项目行（此前每次重跑都新建"项目"，
    # 280 任务散在几十个单任务组里——列表分组失去意义）；
    # 写连接用 write_conn（安全审计实锤：连接不 close 时 500 悬挂持锁雪崩）
    from ..store.projects import ensure_project
    with write_conn() as conn:
        pid, name, _created = ensure_project(conn, user["user_id"], repo_url)
        tid = conn.execute(
            """INSERT INTO test_tasks(project_id, user_id, requirement_text, auto_prepare,
                                      two_stage, ai_reviewer) VALUES (?,?,?,?,?,?)""",
            (pid, user["user_id"], requirement, auto_prepare, two_stage, ai_reviewer)).lastrowid
    return {"task_id": tid, "project_name": name}


@app.post("/api/tasks/{tid}/confirm")
def api_confirm(tid: int, user: dict = Depends(staff)):
    """确认门（fail-closed 结构不变）。两阶段任务：确认=授权+自动启动阶段一
    （克隆/摸底/考纲/出题），跑完停在 ready_for_review 等审查；单阶段任务行为逐字节不变。"""
    conn = get_conn()
    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "任务执行中，禁止变更确认状态")
    # A3（必做项工作包）：D1 准入代审扩展到单任务入口——auto 任务确认时先走 AI 意见书，
    # approve 才置 confirmed_by=agent:reviewer-*；reject/fail-closed 停在未确认交人工
    d1_done = False
    if row["ai_reviewer"] == "auto" and row["confirmed"] != 1:
        from ..skills.ai_reviewer import reviewer_llm, run_d1_review
        d1 = None
        try:
            d1 = run_d1_review(reviewer_llm(), conn, tid,
                               row["requirement_text"] or "",
                               conn.execute("SELECT repo_url FROM projects WHERE id=?",
                                            (row["project_id"],)).fetchone()["repo_url"])
        except Exception as e:  # noqa: BLE001 —— D1 失败 fail-closed 停等人工
            log.warning("单任务 D1 代审失败 task=%s: %s: %s", tid, type(e).__name__, e)
        if d1 is None or d1["decision"] != "approve":
            auth.record_event(user["username"], "failed", user["user_id"])
            raise HTTPException(409, "D1 准入代审未通过（" +
                                (d1["reasoning"][:150] if d1 else "代审不可用/输出不可解析") +
                                "）——任务保持未确认，人工复核后可再次确认")
        conn.execute(
            "UPDATE test_tasks SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
            (f"agent:reviewer-{tid}-d1", tid))
        conn.commit()
        d1_done = True
    else:
        conn.execute(
            "UPDATE test_tasks SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
            (user["user_id"], tid))
        conn.commit()
    started = False
    if row["two_stage"] == 1 and row["status"] not in ("ready_for_review",):
        # 阶段一入队（hybrid/off：跑完停等人工；auto：停等点由代审接管——见 _prep_worker）
        _enqueue(tid, "prep")
        started = True
    return {"task_id": tid, "confirmed": 1, "stage1_started": started, "d1_by_ai": d1_done}


def _enqueue(tid: int, kind: str) -> dict:
    """B1：把工作交给执行队列（单 worker 串行），不再每任务开一个线程同时跑。
    并发下发时任务排队而不是互挤容器/SQLite。"""
    from ..store.queue import start_worker
    start_worker()
    from ..store.queue import QueueWorker
    qid = QueueWorker.enqueue(tid, kind)
    return {"queued": True, "queue_id": qid,
            "position": QueueWorker.pending_count()}


def _prep_worker(tid: int) -> None:
    """阶段一后台线程：准备图跑完停在 ready_for_review；auto 模式在此接 AI 代审钩子。"""
    try:
        from ..graph.builder import run_task_prep
        run_task_prep(tid)
        conn = get_conn()
        row = conn.execute("SELECT ai_reviewer, status FROM test_tasks WHERE id=?", (tid,)).fetchone()
        if row and row["status"] == "ready_for_review" and row["ai_reviewer"] == "hybrid":
            from ..skills.ai_reviewer import reviewer_llm, run_review_once
            try:
                summary = run_review_once(reviewer_llm(), conn, tid, round_no=1)
                if summary:
                    log.info("hybrid 代审完成 task=%s: %s", tid,
                             {k: summary[k] for k in ("approved", "deleted", "regenerate", "inserted")})
            except Exception as e:  # noqa: BLE001 —— 代审失败=无决策落库，人工照常审查
                log.warning("hybrid 代审失败 task=%s: %s: %s", tid, type(e).__name__, e)
    except Exception as e:  # run_task_prep 已把失败落库，这里只防线程栈崩溃噪音
        log.error("后台阶段一任务 %s 异常: %s: %s", tid, type(e).__name__, e)


def _exec_worker(tid: int) -> None:
    try:
        from ..graph.builder import run_task_exec
        run_task_exec(tid)
    except Exception as e:
        log.error("后台阶段二任务 %s 异常: %s: %s", tid, type(e).__name__, e)


def _auto_worker(tid: int) -> None:
    try:
        from ..graph.builder import run_task_auto
        run_task_auto(tid)
    except Exception as e:
        log.error("后台代审链任务 %s 异常: %s: %s", tid, type(e).__name__, e)


@app.post("/api/tasks/{tid}/run")
def api_run(tid: int, user: dict = Depends(staff)):
    """下发执行。两阶段任务：仅待审查态可放行阶段二（执行图）；单阶段任务行为逐字节不变。"""
    conn = get_conn()
    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "任务正在执行中")
    if row["confirmed"] != 1:
        raise HTTPException(409, "任务未确认（fail-closed 确认门），禁止下发")
    if row["two_stage"] == 1:
        if row["status"] != "ready_for_review":
            raise HTTPException(409, "两阶段任务需先完成阶段一（确认后自动跑准备阶段），"
                                     "当前状态：" + row["status"])
        # L1 环境守卫：Docker/工作区缺失时自动开始补齐，不把"环境没准备"留到图里炸
        env_gate()
        q = _enqueue(tid, "exec")
        return {"task_id": tid, "started": True, "stage": 2, **q}
    env_gate()
    q = _enqueue(tid, "exec")
    return {"task_id": tid, "started": True, **q}


def _run_worker(tid: int) -> None:
    try:
        if config.LEVELS_ENABLED:      # D9 串行任务接力：L1 后自动接力 L2（默认关，向后兼容）
            from ..graph.levels import run_task_levels
            run_task_levels(tid, levels=("L1", "L2", "L3") if config.LEVELS_L3_ENABLED
                            else ("L1", "L2"))
        else:
            run_task(tid)
    except Exception as e:  # run_task 已把失败落库，这里只防线程栈崩溃噪音
        log.error("后台执行任务 %s 异常: %s: %s", tid, type(e).__name__, e)


@app.post("/api/tasks/{tid}/stop")
def api_stop(tid: int, user: dict = Depends(staff)):
    conn = get_conn()
    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        # v1 不杀运行中的图执行；但进程死亡会把任务留在执行态"变砖"——管理员强制解卡
        if user["role"] != "admin":
            raise HTTPException(409, "运行中任务 v1 不支持强制停止；管理员可强制解卡")
        conn.execute(
            "UPDATE test_tasks SET status='failed', error='管理员强制停止（解卡）', updated_at=datetime('now') WHERE id=?",
            (tid,))
        conn.commit()
        return {"task_id": tid, "stopped": True, "forced": True}
    conn.execute("UPDATE test_tasks SET status='cancelled', updated_at=datetime('now') WHERE id=? AND status='queued'",
                 (tid,))
    # 排队行同步取消（2026-11-15 闲时找bug）：worker 只看 exec_queue 不复核任务状态，
    # 且 builder 乐观锁不拦 cancelled——不清队列行=已取消任务被"复活"全链执行
    conn.execute(
        "UPDATE exec_queue SET status='cancelled', finished_at=datetime('now'),"
        " error='用户取消（排队中停止）' WHERE task_id=? AND status='queued'", (tid,))
    conn.commit()
    return {"task_id": tid, "stopped": True}


@app.get("/api/tasks")
def api_tasks(user: dict = Depends(current_user)):
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.status, t.confirmed, t.two_stage, t.ai_reviewer,
                  t.requirement_text, t.error, t.created_at, t.updated_at,
                  p.name AS project_name, p.repo_url, p.primary_language,
                  (SELECT COUNT(*) FROM test_cases c
                    JOIN test_scripts sc ON sc.id = c.script_id WHERE sc.task_id = t.id) AS cases,
                  (SELECT r.coverage_line FROM test_reports r WHERE r.task_id = t.id
                    ORDER BY r.id DESC LIMIT 1) AS coverage
           FROM test_tasks t JOIN projects p ON p.id = t.project_id
           ORDER BY t.id DESC""").fetchall()
    return [dict(r, progress=PROGRESS.get(r["status"], 0)) for r in rows]


_TASK_ROW_SQL = """SELECT t.id, t.status, t.confirmed, t.two_stage, t.ai_reviewer,
              t.requirement_text, t.error, t.created_at, t.updated_at, t.project_id,
              p.name AS project_name, p.repo_url, p.primary_language,
              (SELECT COUNT(*) FROM test_cases c
                JOIN test_scripts sc ON sc.id = c.script_id WHERE sc.task_id = t.id) AS cases,
              (SELECT r.coverage_line FROM test_reports r WHERE r.task_id = t.id
                ORDER BY r.id DESC LIMIT 1) AS coverage
       FROM test_tasks t JOIN projects p ON p.id = t.project_id"""


@app.get("/api/tasks/page")
def api_tasks_page(limit: int = 50, offset: int = 0, status: str = "", q: str = "",
                   user: dict = Depends(current_user)):
    """B3 分页任务列表（280+ 任务量级下全量返回会拖垮页面）。

    status: 逗号分隔的多状态过滤（如 `running,ready_for_review`）；
    q: 项目名/需求关键词（LIKE，参数化防注入）。
    返回 {items, total, limit, offset, counts}；counts=各状态计数（筛选条数字用，不受分页影响）。
    """
    conn = get_conn()
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    where, args = [], []
    sts = [s.strip() for s in (status or "").split(",") if s.strip()]
    if sts:
        where.append(f"t.status IN ({','.join('?' * len(sts))})")
        args += sts
    if q.strip():
        where.append("(p.name LIKE ? OR t.requirement_text LIKE ? OR CAST(t.id AS TEXT) = ?)")
        like = f"%{q.strip()}%"
        args += [like, like, q.strip()]
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM test_tasks t JOIN projects p ON p.id=t.project_id {wsql}",
        args).fetchone()["n"]
    rows = conn.execute(
        _TASK_ROW_SQL + f" {wsql} ORDER BY t.id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM test_tasks GROUP BY status")}
    return {"items": [dict(r, progress=PROGRESS.get(r["status"], 0)) for r in rows],
            "total": total, "limit": limit, "offset": offset, "counts": counts}


@app.get("/api/dashboard")
def api_dashboard(user: dict = Depends(current_user)):
    """V1 指挥台聚合（一次请求拿全，避免前端拉 280 行全量自己算）。"""
    conn = get_conn()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM test_tasks GROUP BY status")}
    total = sum(counts.values())
    today = conn.execute(
        "SELECT COUNT(*) AS n FROM test_tasks WHERE date(created_at)=date('now')").fetchone()["n"]
    # 通过率：最近 30 个有评估轮次的任务（按任务均值，不按用例加权——口径与报告判决卡一致：
    # 分母含 skipped=passed+failed+skipped；"最近"按最新评估轮次 MAX(e.id) 取——
    # 旧实现按 task_id 倒序，老任务重跑后永远挤不进近 30。2026-11-15 闲时找bug 修正）
    pr = conn.execute(
        """SELECT AVG(rate) AS r FROM (
             SELECT e.task_id, AVG(CASE WHEN e.passed+e.failed+e.skipped>0
                    THEN e.passed*1.0/(e.passed+e.failed+e.skipped) ELSE NULL END) AS rate
             FROM evaluation_rounds e WHERE e.level='L1' GROUP BY e.task_id
             ORDER BY MAX(e.id) DESC LIMIT 30)""").fetchone()["r"]
    # 缺陷 KPI 按任务去重：report 节点每跑一次 INSERT 一行，按行计数=重跑翻倍
    defects = conn.execute(
        """SELECT COUNT(DISTINCT task_id) AS n FROM test_reports
           WHERE defects LIKE '%product_defect%'""").fetchone()["n"]
    tok = conn.execute(
        "SELECT COALESCE(SUM(total_tokens),0) AS t FROM llm_calls "
        "WHERE date(created_at)=date('now')").fetchone()["t"]
    # 在跑：准备阶段与执行阶段都算（前端据此显示实时执行卡）
    running = [dict(r) for r in conn.execute(
        _TASK_ROW_SQL + " WHERE t.status IN ('cloning','analyzing','generating',"
                        " 'provisioning','running','reporting','queued') ORDER BY t.id DESC LIMIT 6")]
    pending = [dict(r) for r in conn.execute(
        _TASK_ROW_SQL + " WHERE t.status='ready_for_review' ORDER BY t.id DESC LIMIT 8")]
    projects = [dict(r) for r in conn.execute(
        """SELECT p.id, p.name, p.primary_language, COUNT(t.id) AS n,
                  SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN t.status='ready_for_review' THEN 1 ELSE 0 END) AS review,
                  SUM(CASE WHEN t.status IN ('cloning','analyzing','generating',
                            'provisioning','running','reporting','queued') THEN 1 ELSE 0 END) AS active,
                  MAX(t.updated_at) AS last
           FROM projects p LEFT JOIN test_tasks t ON t.project_id=p.id
           GROUP BY p.id HAVING n > 0 ORDER BY last DESC LIMIT 8""")]
    feed = [dict(r) for r in conn.execute(
        _TASK_ROW_SQL + " ORDER BY COALESCE(t.updated_at, t.created_at) DESC LIMIT 8")]
    return {
        "kpi": {"total": total, "today": today,
                "pass_rate": round(pr, 3) if pr is not None else None,
                "defects": defects, "tokens_today": tok,
                "review": counts.get("ready_for_review", 0),
                "active": sum(counts.get(s, 0) for s in
                              ("cloning", "analyzing", "generating", "provisioning",
                               "running", "reporting", "queued"))},
        "counts": counts,
        "running": running, "pending_review": pending,
        "projects": projects, "feed": feed,
    }


@app.get("/api/queue")
def api_queue(limit: int = 20, user: dict = Depends(current_user)):
    """B1 执行队列状态（单 worker 串行；前端显示"排队中 N 个"）。"""
    from ..store.queue import QueueWorker
    return {"pending": QueueWorker.pending_count(), "items": QueueWorker.queue_snapshot(limit)}


@app.get("/api/tasks/{tid}")
def api_task_detail(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    row = _task_or_404(conn, tid)
    # F18-a（前端设计分析 §4）：补 project_name + 主语言/占比（projects JOIN）——详情页元信息行用
    proj = conn.execute(
        "SELECT name, primary_language, language_ratios FROM projects WHERE id=?",
        (row["project_id"],)).fetchone()
    out = dict(row, progress=PROGRESS.get(row["status"], 0))
    if proj:
        out["project_name"] = proj["name"]
        out["primary_language"] = proj["primary_language"]
        try:
            out["language_ratios"] = json.loads(proj["language_ratios"])                 if proj["language_ratios"] else None
        except (TypeError, ValueError):
            out["language_ratios"] = None
    return out


@app.get("/api/tasks/{tid}/defects")
def api_defects(tid: int, user: dict = Depends(current_user)):
    """F18-c：per-case 失败归因摘要——任务级三分类 + 日志 FAILED 行（确定性提取；
    nodeid 匹配生成用例名判 origin，无匹配的自带测试失败如实标 origin=repo）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    rep = conn.execute("SELECT defects FROM test_reports WHERE task_id=? "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    try:
        task_defects = json.loads(rep["defects"]) if rep and rep["defects"] else {}
    except (TypeError, ValueError):
        task_defects = {}
    logs = conn.execute(
        """SELECT m.content FROM agent_messages m JOIN agent_sessions s
           ON s.id = m.session_id WHERE s.task_id=? ORDER BY m.id""", (tid,)).fetchall()
    failed_lines = [l["content"] for l in logs if "FAILED" in l["content"]]
    known = {r[0] for r in conn.execute(
        """SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
           WHERE s.task_id=?""", (tid,))}
    items = []
    for line in failed_lines:
        m = re.search(r"FAILED (\S+)", line)
        nodeid = m.group(1) if m else ""
        base = nodeid.split("::")[-1].split("[")[0]
        items.append({"nodeid": nodeid, "origin": "generated" if base in known else "repo",
                      "raw": line[:200]})
    return {"task_level": task_defects, "failed_cases": items,
            "counts": {"generated": sum(1 for i in items if i["origin"] == "generated"),
                       "repo": sum(1 for i in items if i["origin"] == "repo")}}


@app.get("/api/tasks/{tid}/report")
def api_report(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    _task_or_404(conn, tid)
    # B2+（419/687 教训）：需求-能力错配提示上报告页——generate 轮警告消息确定性抓取
    # （LLM 不经手）；无错配任务该字段为 None（前端不渲染卡片）
    warn = conn.execute(
        """SELECT m.content FROM agent_messages m JOIN agent_sessions s
           ON s.id=m.session_id WHERE s.task_id=?
             AND m.content LIKE '%需求-能力对齐警告%' ORDER BY m.id LIMIT 1""",
        (tid,)).fetchone()
    capability_mismatch = (warn["content"].split("警告] ", 1)[1].strip()
                           if warn and "警告] " in warn["content"] else None)
    rows = conn.execute(
        "SELECT * FROM test_reports WHERE task_id=? ORDER BY id DESC", (tid,)).fetchall()
    # W4 热点快照是任务级（重跑整体替换），与各历史报告的 per_file 交叉求值
    hotspots = [dict(r) for r in conn.execute(
        """SELECT file, name, complexity, rank, lineno FROM complexity_hotspots
           WHERE task_id=? ORDER BY complexity DESC""", (tid,))]
    # LLM 成本台账（P1-E）同为任务级累积口径（跨轮次/回炉调用全计入），历史报告行同值
    lc = conn.execute(
        """SELECT COUNT(*) AS calls, SUM(total_tokens) AS total FROM llm_calls
           WHERE task_id=?""", (tid,)).fetchone()
    llm = {"calls": lc["calls"], "total": lc["total"]} if lc and lc["calls"] else None
    # D9 执行信息（教科书"测试执行阶段"要素）：执行时间/执行方式/轮次——数据都在库里，
    # 此前呈现层没展示，外行看报告不知道"何时、谁、怎么执行的"
    t_row = conn.execute("SELECT status, created_at FROM test_tasks WHERE id=?", (tid,)).fetchone()
    started = conn.execute(
        """SELECT MIN(m.created_at) AS ts FROM agent_messages m
           JOIN agent_sessions s ON s.id = m.session_id WHERE s.task_id=?""", (tid,)).fetchone()
    rounds = conn.execute("SELECT COUNT(*) AS n FROM evaluation_rounds WHERE task_id=?", (tid,)).fetchone()
    exec_meta = {
        "started_at": started["ts"] if started else None,
        "task_created_at": t_row["created_at"] if t_row else None,
        "mode": "全自动（R-Loop 循环执行；缺陷如实上报不回炉迁就）",
        "sandbox": "Docker 容器沙箱",
        "llm": config.DEEPSEEK_MODEL,
        "rounds": rounds["n"] if rounds else 0,
    }
    reports = []
    for r in rows:
        d = dict(r)
        total = (d["passed"] or 0) + (d["failed"] or 0) + (d["skipped"] or 0)
        d["total"] = total
        d["pass_rate"] = round(d["passed"] / total, 4) if total else None
        d["exec"] = exec_meta
        if hotspots:
            cross = hotspot_coverage(hotspots,
                                     json.loads(d["per_file"]) if d["per_file"] else [])
            d["hotspots"] = {"total": cross["total"], "uncovered": cross["uncovered"]}
        if llm:
            d["llm"] = llm
        if capability_mismatch:          # B2+：错配随每份报告行下发（前端渲染警告卡）
            d["capability_mismatch"] = capability_mismatch
        reports.append(d)
    return reports


@app.get("/api/tasks/{tid}/module-profiles")
def api_module_profiles(tid: int, user: dict = Depends(current_user)):
    """模块级画像与策略建议（任务卡-E A 版，只看与建议不改执行链）。
    任务→项目→module_profiles（第 34 表）；未开启 MTA_MODULE_PLAN 的项目返回
    空表（前端不渲染卡片=诚实）。挂 current_user：画像属报告数据（caller 可读）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    t = conn.execute("SELECT project_id FROM test_tasks WHERE id=?", (tid,)).fetchone()
    rows = conn.execute(
        """SELECT path, profile_json, suggested_routes, reason, weight, created_at
           FROM module_profiles WHERE project_id=? ORDER BY weight DESC, path""",
        (t["project_id"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["profile"] = json.loads(d.pop("profile_json") or "{}")
        d["suggested_routes"] = json.loads(d.pop("suggested_routes") or "[]")
        out.append(d)
    return {"task_id": tid, "modules": out}


@app.get("/api/tasks/{tid}/analysis")
def api_get_analysis(tid: int, user: dict = Depends(current_user)):
    """AI 缺陷分析缓存（最新报告行）+ 确定性证据包（失败行/预期/归因，零 LLM，总是可用）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    from ..skills.defect_analysis import collect_failure_evidence
    rep = conn.execute("SELECT analysis FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    cached = _safe_json(rep["analysis"]) if rep and rep["analysis"] else None
    return {"analysis": cached, "evidence": collect_failure_evidence(conn, tid)}


@app.post("/api/tasks/{tid}/analysis")
def api_run_analysis(tid: int, user: dict = Depends(staff)):
    """按需生成 AI 缺陷分析与修复建议（花 LLM token，按钮触发；结果缓存）。
    无 Key 409（诚实）；无失败用例 200 skipped；输出不可解析 200 error（不硬造）。"""
    conn = get_conn()
    row = _task_or_404(conn, tid)
    from ..skills.llm_profile import current, effective_key, missing_key_message
    api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析
    if not api_key:
        raise HTTPException(409, missing_key_message(current())
                            + "，无法生成 AI 分析（确定性证据仍可在概览查看）")
    proj = conn.execute("SELECT primary_language FROM projects WHERE id=?", (row["project_id"],)).fetchone()
    # 缓存短路：同任务重复 POST 不重复烧 token（UI 按钮消失防了界面重复，API 层也要幂等）
    rep = conn.execute("SELECT analysis FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    if rep and rep["analysis"]:
        cached = _safe_json(rep["analysis"])
        if cached:
            return cached
    from ..skills.defect_analysis import run_defect_analysis
    out = run_defect_analysis(api_key, conn, tid,
                              ws=Path(config.DATA_DIR) / "workspaces" / f"task_{tid}",
                              lang=(proj["primary_language"] if proj else None) or "python")
    return out


@app.get("/api/tasks/{tid}/charts")
def api_charts(tid: int, user: dict = Depends(current_user)):
    """报告图表内嵌（gen_charts 产物）：base64 data URL 返回，随报告页一起看。

    图文件在 data/reports/charts/task_{tid}_*.png，任务未跑评估阶段时为空数组。
    """
    conn = get_conn()
    _task_or_404(conn, tid)
    titles = {"rtm": "RTM 需求覆盖", "coverage_trend": "覆盖率趋势（R-Loop 轮次）",
              "verdicts": "R-Loop 判定分布", "tools": "评估工具耗时"}
    out = []
    charts_dir = config.DATA_DIR / "reports" / "charts"
    if charts_dir.exists():
        for p in sorted(charts_dir.glob(f"task_{tid}_*.png")):
            key = p.stem.replace(f"task_{tid}_", "")
            out.append({"name": p.name, "title": titles.get(key, key),
                        "data_url": "data:image/png;base64,"
                                    + base64.b64encode(p.read_bytes()).decode()})
    return out


@app.get("/api/tasks/{tid}/handoff")
def api_handoff(tid: int, user: dict = Depends(current_user)):
    """缺陷交接（用户 2026-11-04「测试→开发/AI 迭代闭环」）：确定性导出两份交付物文本。

    - report=标准缺陷报告单（交开发）；prompt=AI 修复提示词（复制即用，带三护栏）。
    - 无疑似产品缺陷 → 404 诚实告知（不造文档）；内容全部来自 SQL 与缓存，零 LLM。
    """
    conn = get_conn()
    _task_or_404(conn, tid)
    try:
        built = build_handoff(conn, tid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if built is None:
        raise HTTPException(status_code=404,
                            detail="该任务无疑似产品缺陷（generated 用例无失败行），无需交接")
    return built


@app.post("/api/tasks/{tid}/verify-fix")
def api_verify_fix(tid: int, user: dict = Depends(staff)):
    # 变更类端点=staff 专属（同 confirm/run/stop/cases/feedback；caller 仅新建·列表·报告
    # ——2026-11-15 闲时找bug 对齐权限表，旧挂 current_user=caller 可创建派生任务）
    """F（任务卡-369 20261105）回归验证闭环入口：创建验证任务（复制原任务最终用例集，
    requirement 软链前缀，状态直置 ready_for_review——走阶段二执行图=执行-only，
    gen 不跑=用例冻结）。幂等：未执行的验证任务已存在则复用。
    本地目录项目请先确认修复已落盘（快照即新版）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    from ..skills.verify_fix import create_verify_task
    vid = create_verify_task(conn, tid)
    row = conn.execute("SELECT id, status, requirement_text FROM test_tasks WHERE id=?",
                       (vid,)).fetchone()
    return {"verify_task_id": vid, "status": row["status"],
            "requirement": row["requirement_text"],
            "note": "验证任务已就绪（用例冻结）。请从任务列表对其下发执行（阶段二）；"
                    "执行完成后按缺陷组签名出 fail-to-pass 三态判定"}


@app.get("/api/tasks/{tid}/rtm")
def api_rtm(tid: int, user: dict = Depends(current_user)):
    """需求追踪矩阵（RTM）：测试条件 ↔ 用例映射与覆盖状态 + 用例六要素。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    conditions = [dict(r) for r in conn.execute(
        "SELECT id, requirement_ref, condition, priority, method_hint FROM test_conditions "
        "WHERE task_id=? ORDER BY id", (tid,))]
    latest = conn.execute("SELECT matrix, source, created_at FROM rtm_maps "
                          "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    cases = [dict(r) for r in conn.execute(
        """SELECT c.id, c.name, c.method, c.priority, c.input, c.steps, c.expected,
                  c.expected_basis, c.source, c.enabled, c.content
           FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.enabled=1 ORDER BY c.id""", (tid,))]
    if latest:
        matrix = json.loads(latest["matrix"])
    else:
        # 任务B：无映射快照（评估未跑）≠ 条件不存在——用已落库条件构建「未映射」行，
        # 前端标「未映射（未评估）」中性态；不写 rtm_maps（不伪造快照，map_source=None）
        matrix = [{"condition": c["condition"], "priority": c["priority"],
                   "cases": [], "status": "unmapped"} for c in conditions]
    return {"conditions_total": len(conditions),
            "matrix": matrix,
            "map_source": latest["source"] if latest else None,
            "cases": cases}


@app.get("/api/tasks/{tid}/execution")
def api_execution(tid: int, user: dict = Depends(current_user)):
    """任务C：实时执行看板——任务态 + 逐用例结果（test_case_results）+ 轮次轨迹 + 日志尾。
    粒度=轮次级（每轮跑完出现该轮明细，granularity 字段如实标注），前端 2s 轮询。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    from ..skills.case_results import execution_snapshot
    snap = execution_snapshot(conn, tid)
    if "error" in snap:
        raise HTTPException(404, "任务不存在")
    return snap


@app.get("/api/tasks/{tid}/app_logs")
def api_app_logs(tid: int, round: int | None = None,
                 user: dict = Depends(current_user)):
    """黑盒能力补齐阶段1：应用运行时日志分档（app_logs）——被测服务进程 stdout/stderr
    的确定性错误/警告行账本。按轮概要（错误/警告计数、首错行、判源、快照路径）+ 逐条
    证据行；`?round=N` 过滤单轮，不带参数返回全部轮次。纯 SQL 确定性，零 LLM。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    from ..skills.app_logs import logs_snapshot
    return logs_snapshot(conn, tid, round)


@app.get("/api/tasks/{tid}/trace")
def api_trace(tid: int, scenario_id: int | None = None,
              user: dict = Depends(current_user)):
    """四级 RTM 跨级溯源链（P2，E7）：验收→系统→集成→单元→代码位置，纯确定性。

    - `?scenario_id=` 指定场景：返回单条链（不属于该任务 → 404）
    - 不带参数：仅对 pending_confirm / confirmed_defect 场景逐条建链（auto_pass 不触发）
    """
    conn = get_conn()
    _task_or_404(conn, tid)
    try:
        if scenario_id is not None:
            return build_trace_chain(conn, tid, scenario_id)
        return {"task_id": tid, "chains": build_task_traces(conn, tid)}
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/tasks/{tid}/levels")
def api_levels(tid: int, user: dict = Depends(current_user)):
    """D10 后端：四级卡片数据（级别台账 + 按级场景指标 + 失败场景溯源链摘要）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    levels = []
    for r in conn.execute(
            "SELECT id, level, strategy, status, verdict, stop_reason, trace_from,"
            " summary, started_at, finished_at FROM test_levels WHERE task_id=?"
            " ORDER BY CASE level WHEN 'L1' THEN 1 WHEN 'L2' THEN 2"
            " WHEN 'L3' THEN 3 ELSE 4 END, id", (tid,)):
        d = dict(r)
        try:
            d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        except (TypeError, ValueError):
            pass
        levels.append(d)
    if not levels:
        # D6（2026-10-30 H1 实锤）：未走 levels 流的直接 L1 任务在四级视图是空壳——
        # 从任务自身台账合成 L1 回退卡（只读既有数据，不写库；有级别行的任务不受影响）
        t = conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
        latest = conn.execute(
            """SELECT passed, failed, coverage_line, verdict, stop_reason
               FROM evaluation_rounds WHERE task_id=? AND level='L1'
               ORDER BY round_no DESC, id DESC LIMIT 1""", (tid,)).fetchone()
        cases = conn.execute(
            """SELECT COUNT(*) AS n FROM test_cases c
               JOIN test_scripts s ON s.id = c.script_id WHERE s.task_id=?""",
            (tid,)).fetchone()
        if t and (latest or (cases and cases["n"])):
            levels.append({
                "level": "L1", "strategy": "full", "status": t["status"],
                "verdict": latest["verdict"] if latest else None,
                "stop_reason": latest["stop_reason"] if latest else None,
                "trace_from": None, "synthetic": True,
                "summary": {"passed": latest["passed"] if latest else None,
                            "failed": latest["failed"] if latest else None,
                            "coverage_line": latest["coverage_line"] if latest else None,
                            "verdict": latest["verdict"] if latest else None,
                            "cases_total": cases["n"] if cases else 0},
                "started_at": None, "finished_at": None})
    from ..skills.scenario_gen import l3_metrics, l4_metrics
    scenarios = {"L3": l3_metrics(conn, tid), "L4": l4_metrics(conn, tid)}
    traces = build_task_traces(conn, tid)
    return {"task_id": tid, "levels": levels, "scenarios": scenarios,
            "trace_count": len(traces),
            "trace_chains": traces}


@app.get("/api/tasks/{tid}/logs")
def api_logs(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    _task_or_404(conn, tid)
    rows = conn.execute(
        """SELECT m.seq, m.role, m.content, m.created_at FROM agent_messages m
           JOIN agent_sessions s ON s.id = m.session_id
           WHERE s.task_id = ? ORDER BY s.id DESC, m.seq ASC""", (tid,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 脚本工作台（用例查看 / 人工插入） ----------

@app.get("/api/tasks/{tid}/cases")
def api_cases(tid: int, all: bool = False, user: dict = Depends(current_user)):
    """任务用例查询。默认只回最新脚本（老消费方零变化）；?all=1 回任务全量——
    评审页用：反馈增补写在独立 test_gen_fbN（version 更高），只看最新脚本会把
    原始用例整个藏掉，用户分不清哪些是新增（任务A实锤）。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    if all:
        scripts = conn.execute(
            "SELECT id, framework, version, filename FROM test_scripts WHERE task_id=? "
            "ORDER BY version", (tid,)).fetchall()
        out_scripts = []
        for s in scripts:
            cs = conn.execute(
                """SELECT id, name, method, source, enabled, priority, input, expected,
                          expected_basis, content FROM test_cases
                   WHERE script_id=? ORDER BY id""", (s["id"],)).fetchall()
            out_scripts.append({
                "id": s["id"], "framework": s["framework"], "version": s["version"],
                "filename": s["filename"] or "",
                "cases": [dict(c, content=c["content"][:500]) for c in cs]})
        flat = [dict(c, script_id=sc["id"], version=sc["version"],
                     filename=sc["filename"] or "")
                for sc in out_scripts for c in sc["cases"]]
        return {"scripts": out_scripts, "cases": flat}
    script = conn.execute(
        "SELECT id, framework, version, filename, content FROM test_scripts WHERE task_id=? "
        "ORDER BY version DESC LIMIT 1", (tid,)).fetchone()
    if script is None:
        return {"script": None, "cases": []}
    cases = conn.execute(
        """SELECT id, name, method, source, enabled, priority, input, expected, expected_basis,
                  content FROM test_cases
           WHERE script_id=? ORDER BY id""", (script["id"],)).fetchall()
    return {"script": dict(script), "cases": [dict(c, content=c["content"][:500]) for c in cases]}


@app.post("/api/tasks/{tid}/cases")
def api_add_case(tid: int, body: dict, user: dict = Depends(staff)):
    name, code = str(body.get("name", "")).strip(), str(body.get("code", "")).strip()
    method = str(body.get("method", "")).strip() or None
    if not name or not code:
        raise HTTPException(422, "用例名称与代码必填")
    conn = get_conn()
    # 按项目语言校验形态（晚间实验实锤：JS 项目插 Python 形态被 422，插用例对 Tier2 不可用）
    lang_row = conn.execute(
        """SELECT p.primary_language FROM test_tasks t JOIN projects p ON p.id=t.project_id
           WHERE t.id=?""", (tid,)).fetchone()
    is_js = bool(lang_row) and lang_row["primary_language"] == "javascript"
    js_case = re.search(r"""\b(?:test|it)\s*\(\s*['"]test_\w+""", code)
    if is_js and not js_case:
        raise HTTPException(422, "JS 项目用例需为 jest 形态：test(\"test_用例名\", () => {...})，且用例名以 test_ 开头")
    if not is_js and "def test_" not in code:
        raise HTTPException(422, "代码必须包含 def test_ 开头的测试函数")

    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "任务执行中，禁止编辑用例（避免污染本轮执行）")
    ws = Path(config.DATA_DIR) / "workspaces" / f"task_{tid}"
    if not ws.exists():
        raise HTTPException(409, "工作区不存在（任务尚未运行过），请先下发执行一次")

    # name 与 nodeid 对齐（A5 同款规整）：查重范围 = 任务全域用例名
    # （test_imported.py 合并跨脚本版本的全部 imported 用例，只查最新脚本会漏重名）
    script = conn.execute(
        "SELECT id FROM test_scripts WHERE task_id=? ORDER BY version DESC LIMIT 1", (tid,)).fetchone()
    used = {r["name"] for r in conn.execute(
        """SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id = ?""", (tid,))} if script else set()
    def_name = _sanitize_name(name, len(used), used)
    # A5 同款 def 名重写：name↔nodeid 对齐 + 文件内唯一性真正生效
    m = re.search(r"def\s+\w+", code)
    if m:
        code = code[:m.start()] + f"def {def_name}" + code[m.end():]

    script_id = script["id"] if script else conn.execute(
        "INSERT INTO test_scripts(task_id, framework, version, content, content_hash) "
        "VALUES (?, 'pytest', 1, ?, ?)", (tid, "", "")).lastrowid
    conn.execute(
        "INSERT INTO test_cases(script_id, source, name, method, content) VALUES (?,?,?,?,?)",
        (script_id, "imported", def_name, method, code))
    conn.commit()

    file_content = _rebuild_imported_file(conn, tid, ws)
    if script is None:  # 首个用例创建的脚本行，回填内容与哈希
        conn.execute("UPDATE test_scripts SET content=?, content_hash=? WHERE id=?",
                     (file_content, hashlib.sha256(file_content.encode()).hexdigest()[:16], script_id))
        conn.commit()
    return {"task_id": tid, "case_name": def_name,
            "file": "test_imported.test.js" if is_js else "test_imported.py"}


@app.post("/api/cases/{case_id}/toggle")
def api_toggle_case(case_id: int, user: dict = Depends(staff)):
    """用例启停。imported 用例重建 test_imported.py；AI 生成用例在待审查态可启停
    （两阶段评审的"取消用例"——布考场按审查结论重写生成文件即生效），执行链路中仍不可变。"""
    conn = get_conn()
    row = conn.execute(
        """SELECT c.enabled, c.source, s.id AS script_id, s.task_id FROM test_cases c
           JOIN test_scripts s ON s.id = c.script_id WHERE c.id=?""", (case_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"用例 {case_id} 不存在")
    task = conn.execute("SELECT status FROM test_tasks WHERE id=?", (row["task_id"],)).fetchone()
    if task and task["status"] in ACTIVE:
        raise HTTPException(409, "任务执行中，禁止切换用例状态")
    if row["source"] != "imported" and task["status"] != "ready_for_review":
        raise HTTPException(409, "AI 生成脚本为不可变版本（仅两阶段待审查态可启停）")
    conn.execute("UPDATE test_cases SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, case_id))
    conn.commit()
    ws = Path(config.DATA_DIR) / "workspaces" / f"task_{row['task_id']}"
    if not ws.exists():
        # 工作区不存在（阶段一未完成或已被清理）：评审结论已落库即生效——
        # 库是唯一真源，文件只是投影，布考场（阶段二 provision）会以库为准重建，此处不炸
        return {"case_id": case_id, "enabled": 0 if row["enabled"] else 1, "file_deferred": True}
    if row["source"] == "imported":
        content = _rebuild_imported_file(conn, row["task_id"], ws)
        conn.execute("UPDATE test_scripts SET content=?, content_hash=? WHERE id=?",
                     (content, hashlib.sha256(content.encode()).hexdigest()[:16], row["script_id"]))
        conn.commit()
    else:
        from ..skills.gen_tests import rebuild_gen_files_from_db
        rebuild_gen_files_from_db(conn, row["task_id"], ws)
    return {"case_id": case_id, "enabled": 0 if row["enabled"] else 1}


# ---------- 两阶段评审：对话增补 / AI 代审决策（docs/两阶段设计 + docs/AI代审设计） ----------

@app.post("/api/tasks/{tid}/feedback")
def api_feedback(tid: int, body: dict, user: dict = Depends(staff)):
    """对话框增补（两阶段评审页）：人工反馈 → 定向增补生成（独立 test_gen_fbN.py，
    走同一三道闸，不动已批准用例），预算 FEEDBACK_MAX_ROUNDS 轮；AI 代审的
    regenerate 决策走同一管线（同端点语义）。同步返回增补摘要（评审页刷新用例清单）。"""
    feedback = str(body.get("feedback", "")).strip()
    if not feedback:
        raise HTTPException(422, "反馈文本必填")
    conn = get_conn()
    _task_or_404(conn, tid)
    from ..graph.builder import run_feedback_round
    try:
        summary = run_feedback_round(tid, feedback)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    # 任务A：附新增用例明细（name/method/expected/expected_basis/priority + 来源脚本），
    # 前端据此渲染「本轮新增 N 条」面板——只回 generated 数字用户看不到加了什么
    sid = summary.get("script_id")
    if sid:
        rows = conn.execute(
            """SELECT id, name, method, priority, input, expected, expected_basis
               FROM test_cases WHERE script_id=? ORDER BY id""", (sid,)).fetchall()
        summary["cases"] = [dict(r) for r in rows]
        summary["filename"] = summary.get("file") or ""
    else:
        summary["cases"] = []
    return summary


@app.get("/api/tasks/{tid}/review-decisions")
def api_review_decisions(tid: int, user: dict = Depends(current_user)):
    """审查意见书（AI 代审决策台账）：决策/对象/理由/证据/角色/轮次 + hybrid 抽检标记与复核状态。

    spotcheck: True=该条在最新轮 20% 抽检清单且尚未复核；
    spotcheck_status: done=已被人工复核（附 verdict/结论/时间——从人工落库行反查），
    前端据此把卡片就地变成"已复核"形态。"""
    conn = get_conn()
    _task_or_404(conn, tid)
    rows = [dict(r) for r in conn.execute(
        """SELECT id, decision, target, reasoning, evidence, reviewer_role, round, created_at
           FROM review_decisions WHERE task_id=? ORDER BY id""", (tid,))]
    latest_round = max((r["round"] for r in rows), default=0)
    # 抽样池只含 AI 决策——人工落库行加入后 ids 池变化会让抽检集合重排（E2E 实锤）
    sample = spotcheck_sample(tid, [r["id"] for r in rows
                                    if r["round"] == latest_round and r["reviewer_role"] == "ai_proxy"]) \
        if latest_round else set()
    # 人工抽检落库行（evidence.spotcheck_of=被复核的 AI 决策 id）反查复核状态
    reviewed: dict[int, dict] = {}
    for r in rows:
        if r["reviewer_role"] != "human":
            continue
        ev = _safe_json(r["evidence"]) or {}
        of = ev.get("spotcheck_of")
        if of is not None:
            verdict = ev.get("verdict") or ("uphold" if str(r["reasoning"]).startswith("【抽检同意】") else "override")
            reviewed[of] = {"verdict": verdict, "human_decision": r["decision"],
                            "note": str(r["reasoning"]).split("】", 1)[-1], "at": r["created_at"]}
    for r in rows:
        r["evidence"] = _safe_json(r["evidence"])
        is_ai = r["reviewer_role"] == "ai_proxy"
        r["spotcheck"] = r["id"] in sample and is_ai and r["id"] not in reviewed
        r["spotcheck_status"] = ("done" if r["id"] in reviewed
                                 else "pending" if (r["id"] in sample and is_ai) else None)
        if r["id"] in reviewed:
            r["spotcheck_review"] = reviewed[r["id"]]
    return {"decisions": rows, "spotcheck_ratio": 0.2,
            "pending_spotcheck": sum(1 for r in rows if r["spotcheck"])}


def _safe_json(text):
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


@app.post("/api/review-decisions/{did}/spotcheck")
def api_spotcheck(did: int, body: dict, user: dict = Depends(staff)):
    """hybrid 抽检：uphold=同意 / override=推翻。推翻也落库（对齐信号）；
    推翻"删除"决策同时恢复用例启用（人工意志即时生效）。"""
    conn = get_conn()
    from ..skills.ai_reviewer import record_spotcheck
    try:
        out = record_spotcheck(conn, did, str(body.get("verdict", "")),
                               str(body.get("note", "")), user["user_id"])
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return out


@app.post("/api/tasks/batch/ai-reviewer")
def api_batch_reviewer(body: dict, user: dict = Depends(staff)):
    """批量开关（AI代审设计 §5）：对 N 个任务批量设置 ai_reviewer 模式。
    执行链路中的任务跳过。返回逐任务结果。"""
    mode = str(body.get("mode", "")).strip()
    if mode not in ("off", "hybrid", "auto"):
        raise HTTPException(422, "mode 必须是 off/hybrid/auto")
    ids = body.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "task_ids 必填（数组）")
    conn = get_conn()
    out = []
    for tid in ids:
        row = conn.execute("SELECT status FROM test_tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            out.append({"task_id": tid, "applied": False, "reason": "不存在"})
        elif row["status"] in ACTIVE:
            out.append({"task_id": tid, "applied": False, "reason": "执行中"})
        else:
            conn.execute("UPDATE test_tasks SET ai_reviewer=? WHERE id=?", (mode, tid))
            out.append({"task_id": tid, "applied": True})
    conn.commit()
    return {"mode": mode, "results": out}


@app.post("/api/tasks/batch/auto-run")
def api_batch_auto_run(body: dict, user: dict = Depends(staff)):
    """批量代审链入口：对已开 auto 且未确认的任务启动 D1→阶段一→D2→阶段二 全链。
    任何任务停在等待点都如实返回（fail-closed 交人工），不影响其他任务。"""
    ids = body.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "task_ids 必填（数组）")
    conn = get_conn()
    out = []
    for tid in ids:
        row = conn.execute("SELECT status, confirmed, ai_reviewer FROM test_tasks WHERE id=?",
                           (tid,)).fetchone()
        if row is None:
            out.append({"task_id": tid, "started": False, "reason": "不存在"})
        elif row["ai_reviewer"] != "auto":
            out.append({"task_id": tid, "started": False, "reason": "未开启 auto 代审"})
        elif row["status"] in ACTIVE:
            out.append({"task_id": tid, "started": False, "reason": "执行中"})
        elif row["status"] == "ready_for_review" and row["confirmed"] == 1:
            _enqueue(tid, "exec")
            out.append({"task_id": tid, "started": True, "stage": 2})
        elif row["confirmed"] == 1:
            out.append({"task_id": tid, "started": False, "reason": f"状态 {row['status']} 无需代审"})
        else:
            _enqueue(tid, "auto")
            out.append({"task_id": tid, "started": True, "stage": "auto"})
    return {"results": out}


# ---------- 设置页（仅管理员）：基础配置 / 镜像管理 ----------

@app.get("/api/config")
def api_get_config(user: dict = Depends(admin)):
    return {
        "coverage_target": config.COVERAGE_TARGET,
        "max_repair_rounds": config.MAX_REPAIR_ROUNDS,
        "fingerprint_breaker": config.FINGERPRINT_BREAKER,
        "llm_base_url": config.DEEPSEEK_BASE_URL,   # 运行时现值（llm_profile.apply 改写后）
        "llm_model": config.DEEPSEEK_MODEL,
        "images": {k: str(v) for k, v in config.IMAGE_BUILD_CTX.items()},
        "note": "预算值仍冻结（实验预注册纪律）；LLM 模型可在设置页切换（实验数据以 llm_usage "
                "台账记录的实际模型为准）",
    }


# ---------- LLM 模型 profile（任务卡-LLM模型选择-20261108：三档预设+custom，key 永不入库） ----------

@app.get("/api/llm-profile")
def api_get_llm_profile(user: dict = Depends(staff)):
    """当前 profile + 预设清单（各档含 key_present 徽章数据）+ 当前 key 有无（只报有无，永不回显值）。"""
    import os as _os
    from ..skills.llm_profile import PRESETS, current, resolve_key
    p = current()
    key_present = resolve_key(p) is not None
    presets = {k: {**v, "key_present": bool(v.get("key_env") and _os.environ.get(v["key_env"]))
                   or v.get("provider") == "local"}
               for k, v in PRESETS.items()}
    return {"profile": p, "presets": presets, "key_present": key_present,
            "updated_by": p.get("updated_by") or "", "updated_at": p.get("updated_at") or ""}


@app.post("/api/llm-profile")
def api_set_llm_profile(body: dict, user: dict = Depends(admin)):
    """切换并应用（admin；caller 403 由 Depends(admin) 保证）。返回应用后的完整 profile。"""
    from ..skills.llm_profile import apply
    conn = get_conn()
    try:
        p = apply(body, conn, updated_by=str(user.get("username") or user.get("id") or "admin"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "profile": p}


@app.post("/api/llm-profile/test")
def api_test_llm_profile(body: dict, user: dict = Depends(staff)):
    """连通性测试：body 可带临时 profile（不落库）。返回 {ok, latency_ms, reply|error}。"""
    from ..skills.llm_profile import test_connection
    return test_connection(body or {})


# ---------- 每节点 LLM 可替换（2026-11-10 任务卡 2 期：phase 组 → profile 路由） ----------

@app.get("/api/llm-routing")
def api_get_llm_routing(user: dict = Depends(admin)):
    """profiles + routing + phase 组清单（grep 实测口径）+ 各组近 7 天调用量（诚实纪律：
    让人知道改哪个节点有意义——确定性聚合，不让 LLM 编）。"""
    from ..skills.llm_profile import PHASE_GROUPS, current_doc, phase_group
    doc = current_doc()
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT phase, model, COUNT(*) n FROM llm_calls
               WHERE created_at >= datetime('now', '-7 days') GROUP BY phase, model""")
        usage_raw: dict = {}
        for r in rows:
            g = phase_group(r["phase"])
            d = usage_raw.setdefault(g, {})
            d[r["model"]] = d.get(r["model"], 0) + r["n"]
    finally:
        conn.close()
    usage = {g: {"calls": sum(m.values()), "models": m}
             for g, m in usage_raw.items()}
    return {"default": doc.get("default"), "profiles": doc.get("profiles") or {},
            "routing": doc.get("routing") or {}, "phase_groups": sorted(PHASE_GROUPS),
            "usage_7d": usage}


@app.put("/api/llm-routing")
def api_put_llm_routing(body: dict, user: dict = Depends(admin)):
    """写 routing（phase 组 → profile_id；空串=用 default）。非法组/无效 profile 400。"""
    from ..skills.llm_profile import apply_routing
    conn = get_conn()
    try:
        doc = apply_routing(body.get("routing") or {}, conn,
                            updated_by=str(user.get("username") or "admin"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "default": doc.get("default"),
            "profiles": doc.get("profiles") or {}, "routing": doc.get("routing") or {}}


@app.get("/api/images")
def api_images(user: dict = Depends(staff)):
    """镜像管理动态视图（三源合并去重，2026-11-07）：
    ① registered=IMAGE_BUILD_CTX（buildable=true，可「自动构建」）
    ② local=docker 实际扫描（SDK images.list()，与 docker images --format 等价——
       runner 现有方式即 SDK，非 CLI）
    ③ aux=AUX_IMAGES（sidecar/recipe 基础池，仅展示）
    按镜像名去重，来源优先级 registered > local > aux。docker 不可达 → degraded
    标志随 200 返回（页面仍可用注册/辅助两源），不抛 500。
    新镜像接入：可构建 → 注册 IMAGE_BUILD_CTX；仅展示 → 加 AUX_IMAGES。"""
    def _probe(image: str) -> tuple[bool, int | None]:
        try:
            info = get_runner().client.images.get(image)
            return True, round((info.attrs.get("Size") or 0) / 1e6)
        except Exception:                 # noqa: BLE001 —— ImageNotFound 及驱动差异
            return False, None

    rows: dict[str, dict] = {}
    for image in config.IMAGE_BUILD_CTX:
        exists, size_mb = _probe(image)
        rows[image] = {"name": image, "source": "registered", "exists": exists,
                       "size_mb": size_mb, "buildable": True}
    degraded = ""
    docker_scanned = True
    try:
        listed = get_runner().client.images.list()
    except Exception as e:                # noqa: BLE001 —— docker 未起/守护进程瞬断
        docker_scanned = False
        degraded = f"docker 扫描不可用（{type(e).__name__}），仅返回注册/辅助两源"
        listed = []
    for img in listed:
        for tag in (img.tags or []):
            if tag not in rows:
                rows[tag] = {"name": tag, "source": "local", "exists": True,
                             "size_mb": round((img.attrs.get("Size") or 0) / 1e6),
                             "buildable": False}
    for image in config.AUX_IMAGES:
        if image in rows:
            continue                      # registered/local 优先级更高，不覆盖
        exists, size_mb = _probe(image)
        rows[image] = {"name": image, "source": "aux", "exists": exists,
                       "size_mb": size_mb, "buildable": False}
    out = {"images": sorted(rows.values(), key=lambda r: (r["source"], r["name"])),
           "docker_scanned": docker_scanned}
    if degraded:
        out["degraded"] = degraded
    return out


@app.post("/api/images/{image}/build")
def api_build_image(image: str, user: dict = Depends(admin)):
    if image not in config.IMAGE_BUILD_CTX:
        raise HTTPException(404, f"镜像 {image} 不在注册表")
    ok = get_runner().ensure_image(image)
    if not ok:
        raise HTTPException(500, f"镜像 {image} 构建失败，详见服务端日志")
    return {"image": image, "ready": True}


@app.get("/api/env/status")
def api_env_status(user: dict = Depends(staff)):
    """环境自检快照（只检测不修复）+ 最近一次自动补齐状态。"""
    with _ENV_LOCK:
        state = dict(_ENV_STATE)
    return {"probe": preflight.probe(), "prepare": state}


@app.post("/api/env/prepare")
def api_env_prepare(user: dict = Depends(staff)):
    """一键准备：异步启动 Docker / 构建缺失镜像，前端轮询 /api/env/status 看进度。"""
    return trigger_env_prepare()


# ---------- L3 场景（D5 半自动：失败人工介入） ----------

@app.get("/api/tasks/{tid}/scenarios")
def api_scenarios(tid: int, level: str = "L3", user: dict = Depends(current_user)):
    conn = get_conn()
    _task_or_404(conn, tid)
    rows = conn.execute(
        """SELECT id, title, requirement_ref, steps, expected, gwt, status, last_result,
                  actual_note, confirmed_at FROM scenario_cases
           WHERE task_id=? AND level=? ORDER BY id""", (tid, level)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/tasks/{tid}/l4/convert")
def api_convert_l4(tid: int, user: dict = Depends(staff)):
    """D6 转化器：confirmed L4 验收场景 → 可执行 pytest 回归脚本（source='acceptance'）。

    AST 编译闸失败 → 轻量兜底（场景保留确认态，转化记录 fail，不丢人工结论）。"""
    import re as _re
    conn = get_conn()
    _task_or_404(conn, tid)
    confirmed = [dict(r) for r in conn.execute(
        """SELECT id, title, steps, expected FROM scenario_cases
           WHERE task_id=? AND level='L4' AND status IN
           ('confirmed_pass','confirmed_defect','confirmed_scenario')
           ORDER BY id""", (tid,))]
    if not confirmed:
        raise HTTPException(409, "无已确认的 L4 验收场景可转化（先经确认流转）")
    for sc in confirmed:
        sc["steps"] = json.loads(sc["steps"])
    ws = Path(config.DATA_DIR) / "workspaces" / f"task_{tid}"
    if not ws.exists():      # api_add_case 同款诚实 409（旧=裸 FileNotFoundError 500）
        raise HTTPException(409, "工作区不存在（任务未跑过或已清理），无法转化")
    base_url = f"http://127.0.0.1:{config.L2_SERVICE_PORT}"
    from ..skills.scenario_gen import render_acceptance_module
    try:
        src, cases = render_acceptance_module(confirmed, base_url)
        compile(src, "test_acceptance.py", "exec")            # AST 编译闸
    except (SyntaxError, ValueError, KeyError) as e:
        return {"converted": 0, "fallback": "light",
                "reason": f"渲染/编译失败退轻量兜底：{type(e).__name__}: {e}"[:300]}
    fname = "test_acceptance.py"
    (ws / fname).write_text(src, encoding="utf-8")
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, content, content_hash)
           VALUES (?, 'acceptance',
                   (SELECT COALESCE(MAX(version), 0) + 1 FROM test_scripts
                    WHERE task_id=? AND framework='acceptance'), ?, ?)""",
        (tid, tid, src, __import__("hashlib").sha256(src.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    for c in cases:
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content)
               VALUES (?, 'imported', ?, '验收回归', ?)""",
            (script_id, c["name"], json.dumps(c["steps"], ensure_ascii=False)))
    conn.commit()
    return {"converted": len(cases), "script_id": script_id, "file": fname}


@app.post("/api/scenarios/{sid}/confirm")
def api_confirm_scenario(sid: int, body: dict, user: dict = Depends(staff)):
    from ..skills.scenario_gen import confirm_scenario
    conn = get_conn()
    try:
        out = confirm_scenario(conn, sid, str(body.get("result", "")),
                               body.get("note"), user["user_id"])
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return out


# ---------- 静态界面（置于 API 路由之后） ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _sweep_orphan_containers() -> None:
    """服务重启即视为旧任务已死：回收上次进程死亡残留的 mta-run-* / mta-ui-* 容器（幂等）。
    mta-ui-*：ui_idempotency 每轮新建的 playwright 常驻容器（含 chromium），旧版清扫
    不覆盖 → 无限累积（2026-11-15 闲时找bug）。注意不要放宽成 mta-*——会误杀
    mta-pg 数据库 sidecar。"""
    try:
        for prefix in ("mta-run-", "mta-ui-"):
            for c in get_runner().client.containers.list(filters={"name": prefix}):
                c.remove(force=True)
                log.warning("已回收疑似孤儿容器 %s", c.name)
    except Exception as e:  # Docker 未起等场景不阻断服务启动
        log.warning("孤儿容器清扫跳过: %s", e)


def _sweep_orphan_active_tasks() -> None:
    """服务重启清掉 worker 线程后，ACTIVE 态任务会变砖（348 实锤：卡 cloning 无法确认，
    要 admin 手动解卡）——启动时自动标记 failed 并给出重跑指引（ready_for_review 是
    合法停等态，不清扫）。"""
    conn = get_conn()
    n = 0
    for r in conn.execute(
            f"SELECT id FROM test_tasks WHERE status IN ({','.join('?' * len(ACTIVE))})",
            tuple(ACTIVE)).fetchall():
        conn.execute(
            "UPDATE test_tasks SET status='failed', error='服务重启导致任务中断——请重新确认（会自动重跑对应阶段）', "
            "updated_at=datetime('now') WHERE id=?", (r["id"],))
        n += 1
    conn.commit()
    conn.close()
    if n:
        log.warning("启动清扫：%d 个 ACTIVE 态孤儿任务已标记 failed（服务重启变砖防护）", n)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    auth.ensure_default_users()
    _sweep_orphan_containers()
    _sweep_orphan_active_tasks()
    # B1：启动执行队列 worker（单线程串行；重启把遗留 running 行标中断，queued 继续消费）
    try:
        from ..store.queue import start_worker
        start_worker()
    except Exception as e:      # noqa: BLE001 —— 队列起不来不阻断服务（单项下发仍可走旧路径）
        log.error("执行队列启动失败: %s: %s", type(e).__name__, e)
    log.info("L1 服务就绪：http://127.0.0.1:8000（演示账号 admin/admin123 · dev/dev123 · caller/caller123）")
