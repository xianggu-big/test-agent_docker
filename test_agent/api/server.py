"""L1 应用层：FastAPI REST + 三角色守卫 + 静态界面（阶段 2，按图纸排版）。

约定：
- 鉴权：Bearer token（auth.py 内存 token，重启失效）；角色 admin 全部 / developer 除设置管理 / caller 仅新建·列表·报告
- 下发执行：后台线程跑 run_task，前端轮询 /api/tasks/{tid}；builder 的 A4 乐观锁兜底并发
- 人工插用例：落库 source='imported' + 重建工作区 test_imported.py（下次 run 随快照进容器）；
  name 经 A5 规整保证与 nodeid 一致
- 运行中任务禁止编辑/确认变更；停止仅对未下发任务生效（运行中不可强停，容器随任务回收）

启动：python -m uvicorn test_agent.api.server:app --port 8000  （禁用 --reload 跑真任务）
"""
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
from ..skills.gen_tests import _rebuild_imported_file, _sanitize_name
from ..store import db
from ..store.db import get_conn
from . import auth

log = logging.getLogger(__name__)

ACTIVE = {"cloning", "analyzing", "provisioning", "generating", "running", "reporting"}
PROGRESS = {"queued": 5, "cloning": 10, "analyzing": 25, "provisioning": 40,
            "generating": 55, "running": 70, "reporting": 85, "done": 100,
            "failed": 100, "cancelled": 100}
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
        result = preflight.ensure(auto_repair=True)
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
    """L1 环境守卫：不就绪则触发自动补齐并拒绝下发（409），不把环境问题留到图里炸。"""
    env = preflight.probe()
    if env["ready"]:
        return
    trigger_env_prepare()
    missing = "、".join(i["label"] for i in env["items"]
                        if i["blocking"] and i["status"] != "ok")
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

@app.post("/api/login")
def api_login(body: dict):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        raise HTTPException(422, "用户名与密码必填")
    result = auth.login(username, password)
    if result is None:
        raise HTTPException(401, "用户名或密码错误")
    return result


@app.get("/api/me")
def api_me(user: dict = Depends(current_user)):
    return user


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
    conn = get_conn()
    name = repo_url.rstrip("/").split("/")[-1] or "project"
    pid = conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (?,?,?)",
                       (user["user_id"], repo_url, name)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id, user_id, requirement_text) VALUES (?,?,?)",
        (pid, user["user_id"], requirement)).lastrowid
    conn.commit()
    return {"task_id": tid, "project_name": name}


@app.post("/api/tasks/{tid}/confirm")
def api_confirm(tid: int, user: dict = Depends(staff)):
    conn = get_conn()
    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "任务执行中，禁止变更确认状态")
    conn.execute(
        "UPDATE test_tasks SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
        (user["user_id"], tid))
    conn.commit()
    return {"task_id": tid, "confirmed": 1}


@app.post("/api/tasks/{tid}/run")
def api_run(tid: int, user: dict = Depends(staff)):
    conn = get_conn()
    row = _task_or_404(conn, tid)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "任务正在执行中")
    if row["confirmed"] != 1:
        raise HTTPException(409, "任务未确认（fail-closed 确认门），禁止下发")
    # L1 环境守卫：缺 Docker/镜像/工作区时自动开始补齐，不把"环境没准备"留到图里炸
    env_gate()
    threading.Thread(target=_run_worker, args=(tid,), daemon=True).start()
    return {"task_id": tid, "started": True}


def _run_worker(tid: int) -> None:
    try:
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
    conn.commit()
    return {"task_id": tid, "stopped": True}


@app.get("/api/tasks")
def api_tasks(user: dict = Depends(current_user)):
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.status, t.confirmed, t.requirement_text, t.error, t.created_at,
                  p.name AS project_name, p.repo_url, p.primary_language
           FROM test_tasks t JOIN projects p ON p.id = t.project_id
           ORDER BY t.id DESC""").fetchall()
    return [dict(r, progress=PROGRESS.get(r["status"], 0)) for r in rows]


@app.get("/api/tasks/{tid}")
def api_task_detail(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    row = _task_or_404(conn, tid)
    return dict(row, progress=PROGRESS.get(row["status"], 0))


@app.get("/api/tasks/{tid}/report")
def api_report(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    _task_or_404(conn, tid)
    rows = conn.execute(
        "SELECT * FROM test_reports WHERE task_id=? ORDER BY id DESC", (tid,)).fetchall()
    reports = []
    for r in rows:
        d = dict(r)
        total = (d["passed"] or 0) + (d["failed"] or 0) + (d["skipped"] or 0)
        d["total"] = total
        d["pass_rate"] = round(d["passed"] / total, 4) if total else None
        reports.append(d)
    return reports


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
        """SELECT c.name, c.method, c.priority, c.input, c.steps, c.expected,
                  c.expected_basis, c.source, c.enabled
           FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.enabled=1 ORDER BY c.id""", (tid,))]
    return {"conditions_total": len(conditions),
            "matrix": json.loads(latest["matrix"]) if latest else [],
            "map_source": latest["source"] if latest else None,
            "cases": cases}


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
def api_cases(tid: int, user: dict = Depends(current_user)):
    conn = get_conn()
    _task_or_404(conn, tid)
    script = conn.execute(
        "SELECT id, framework, version, content FROM test_scripts WHERE task_id=? "
        "ORDER BY version DESC LIMIT 1", (tid,)).fetchone()
    if script is None:
        return {"script": None, "cases": []}
    cases = conn.execute(
        "SELECT id, name, method, source, enabled, content FROM test_cases "
        "WHERE script_id=? ORDER BY id", (script["id"],)).fetchall()
    return {"script": dict(script), "cases": [dict(c, content=c["content"][:500]) for c in cases]}


@app.post("/api/tasks/{tid}/cases")
def api_add_case(tid: int, body: dict, user: dict = Depends(staff)):
    name, code = str(body.get("name", "")).strip(), str(body.get("code", "")).strip()
    method = str(body.get("method", "")).strip() or None
    if not name or not code:
        raise HTTPException(422, "用例名称与代码必填")
    if "def test_" not in code:
        raise HTTPException(422, "代码必须包含 def test_ 开头的测试函数")

    conn = get_conn()
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
    return {"task_id": tid, "case_name": def_name, "file": "test_imported.py"}


@app.post("/api/cases/{case_id}/toggle")
def api_toggle_case(case_id: int, user: dict = Depends(staff)):
    """用例启停：imported 用例重建工作区文件即生效；AI 生成脚本为不可变版本，v1 不支持。"""
    conn = get_conn()
    row = conn.execute(
        """SELECT c.enabled, c.source, s.id AS script_id, s.task_id FROM test_cases c
           JOIN test_scripts s ON s.id = c.script_id WHERE c.id=?""", (case_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"用例 {case_id} 不存在")
    task = conn.execute("SELECT status FROM test_tasks WHERE id=?", (row["task_id"],)).fetchone()
    if task and task["status"] in ACTIVE:
        raise HTTPException(409, "任务执行中，禁止切换用例状态")
    if row["source"] != "imported":
        raise HTTPException(409, "v1 仅支持人工/导入用例启停；AI 生成脚本为不可变版本")
    conn.execute("UPDATE test_cases SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, case_id))
    conn.commit()
    content = _rebuild_imported_file(conn, row["task_id"],
                                     Path(config.DATA_DIR) / "workspaces" / f"task_{row['task_id']}")
    conn.execute("UPDATE test_scripts SET content=?, content_hash=? WHERE id=?",
                 (content, hashlib.sha256(content.encode()).hexdigest()[:16], row["script_id"]))
    conn.commit()
    return {"case_id": case_id, "enabled": 0 if row["enabled"] else 1}


# ---------- 设置页（仅管理员）：基础配置 / 镜像管理 ----------

@app.get("/api/config")
def api_get_config(user: dict = Depends(admin)):
    return {
        "coverage_target": config.COVERAGE_TARGET,
        "max_repair_rounds": config.MAX_REPAIR_ROUNDS,
        "fingerprint_breaker": config.FINGERPRINT_BREAKER,
        "llm_base_url": config.DEEPSEEK_BASE_URL,
        "llm_model": config.DEEPSEEK_MODEL,
        "images": {k: str(v) for k, v in config.IMAGE_BUILD_CTX.items()},
        "note": "v1 配置只读：实验预注册纪律要求预算值冻结，修改请改 config.py 后重启",
    }


@app.get("/api/images")
def api_images(user: dict = Depends(staff)):
    out = []
    for image in config.IMAGE_BUILD_CTX:
        try:
            info = get_runner().client.images.get(image)
            out.append({"image": image, "ready": True, "size_mb": round(info.attrs["Size"] / 1e6)})
        except Exception:
            out.append({"image": image, "ready": False, "size_mb": None})
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


# ---------- 静态界面（置于 API 路由之后） ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _sweep_orphan_containers() -> None:
    """服务重启即视为旧任务已死：回收上次进程死亡残留的 mta-run-* 容器（幂等）。"""
    try:
        for c in get_runner().client.containers.list(filters={"name": "mta-run-"}):
            c.remove(force=True)
            log.warning("已回收疑似孤儿容器 %s", c.name)
    except Exception as e:  # Docker 未起等场景不阻断服务启动
        log.warning("孤儿容器清扫跳过: %s", e)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    auth.ensure_default_users()
    _sweep_orphan_containers()
    log.info("L1 服务就绪：http://127.0.0.1:8000（演示账号 admin/admin123 · dev/dev123 · caller/caller123）")
