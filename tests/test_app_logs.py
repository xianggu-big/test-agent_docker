"""黑包阶段1·运行时日志分流与 log_analyze（docs/黑盒能力补齐任务包-20261106.md 阶段1）。

测试矩阵（任务卡「测试矩阵」节，正例+负例，闸门类负例优先）：
1) 四语言错误行正则各≥1正1负（Python Traceback / Java 堆栈 / Go panic / Node rejection）
2) log_analyze 引用不存在行号 → 必须失败（防 vibes 叙述）
3) 日志分流路径拼接
4) app_logs 落库幂等（重跑不重复计行）
"""
from pathlib import Path

from test_agent import config
from test_agent.store import db


# ---------- 样本日志（行号即断言依据，勿改顺序） ----------

PY_LOG = "\n".join([
    "INFO:     Started server process [1]",                                   # 1
    "INFO:     Application startup complete.",                                # 2
    "ERROR:    [Errno 98] error while attempting to bind on address",          # 3
    "Traceback (most recent call last):",                                     # 4
    '  File "/work/app.py", line 10, in handler',                             # 5（ traceback 体不算错误行）
    "RuntimeError: boom",                                                     # 6
    "WARNING:  some warning text",                                            # 7
])

JAVA_LOG = "\n".join([
    "INFO: Starting Spring Boot application",                                 # 1
    "java.lang.NullPointerException: name is null",                           # 2
    "    at com.demo.OrderService.validate(OrderService.java:42)",            # 3
    "    at com.demo.OrderController.create(OrderController.java:88)",        # 4
    "Caused by: java.lang.IllegalArgumentException: name must not be null",    # 5
])

GO_LOG = "\n".join([
    "2026/11/06 10:00:00 Serving on :8080",                                   # 1
    "panic: runtime error: index out of range [5] with length 3",             # 2
    "goroutine 1 [running]:",                                                 # 3
    "main.sumAll(...)",                                                       # 4（帧体不算）
    "\t/work/main.go:12",                                                     # 5（帧体不算）
])

NODE_LOG = "\n".join([
    "INFO: server listening on 3000",                                         # 1
    "(node:1) UnhandledPromiseRejection: Error: query failed",                # 2
    "    at /work/server.js:41:18",                                           # 3（帧体不算）
    "TypeError: Cannot read properties of undefined (reading 'id')",          # 4
])


def _seed_task(conn) -> int:
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,?)",
                       (pid, uid)).lastrowid
    conn.commit()
    return tid


# ---------- 1) 四语言正则：正例 ----------

def test_python_traceback_positive():
    from test_agent.skills.app_logs import parse_app_log
    entries = parse_app_log(PY_LOG, "python")
    err_lines = [e["line_no"] for e in entries if e["level"] == "error"]
    assert sorted(err_lines) == [3, 4, 6]        # ERROR 行 / Traceback 头 / 异常尾行
    kinds = {e["kind"] for e in entries}
    assert "python_traceback" in kinds
    warn = [e for e in entries if e["level"] == "warn"]
    assert [e["line_no"] for e in warn] == [7]


def test_java_stack_positive():
    from test_agent.skills.app_logs import parse_app_log
    entries = parse_app_log(JAVA_LOG, "java")
    assert sorted(e["line_no"] for e in entries) == [2, 3, 4, 5]
    kinds = {e["kind"] for e in entries}
    assert "java_exception" in kinds and "java_stack" in kinds and "java_caused_by" in kinds


def test_go_panic_positive():
    from test_agent.skills.app_logs import parse_app_log
    entries = parse_app_log(GO_LOG, "go")
    assert sorted(e["line_no"] for e in entries) == [2, 3]
    kinds = {e["kind"] for e in entries}
    assert "go_panic" in kinds
    assert all(e["level"] == "error" for e in entries)


def test_node_rejection_positive():
    from test_agent.skills.app_logs import parse_app_log
    entries = parse_app_log(NODE_LOG, "javascript")
    assert sorted(e["line_no"] for e in entries) == [2, 4]
    kinds = {e["kind"] for e in entries}
    assert "node_rejection" in kinds and "node_error" in kinds


# ---------- 1) 四语言正则：负例（安静日志必须零命中） ----------

def test_python_negative_quiet_log():
    from test_agent.skills.app_logs import parse_app_log
    quiet = 'INFO:     127.0.0.1 - "GET /x HTTP/1.1" 200 OK\nINFO:     200 OK'
    assert parse_app_log(quiet, "python") == []


def test_java_negative_quiet_log():
    from test_agent.skills.app_logs import parse_app_log
    quiet = "INFO: Starting Spring Boot application\nTomcat started on port(s): 8080"
    assert parse_app_log(quiet, "java") == []


def test_go_negative_quiet_log():
    from test_agent.skills.app_logs import parse_app_log
    quiet = "2026/11/06 10:00:00 Serving on :8080\n[GIN] 200 | 1.2ms | GET /health"
    assert parse_app_log(quiet, "go") == []


def test_node_negative_quiet_log():
    from test_agent.skills.app_logs import parse_app_log
    quiet = "INFO: server listening on 3000\nrequest GET /api ok"
    assert parse_app_log(quiet, "javascript") == []


# ---------- 2) log_analyze 引用闸（防 vibes，负例优先） ----------

def test_validate_citations_rejects_nonexistent_line():
    from test_agent.skills.log_analyze import validate_citations
    ok, invalid = validate_citations("错误发生在行 999 处", valid_max_line=20)
    assert ok is False and invalid == [999]


def test_validate_citations_accepts_real_line():
    from test_agent.skills.log_analyze import validate_citations
    ok, invalid = validate_citations("行 3 出现绑定失败，行 7 有警告", valid_max_line=20)
    assert ok is True and invalid == []


def test_validate_citations_requires_at_least_one():
    """有错误条目却零引用 → 不通过（叙述必须锚定证据行）。"""
    from test_agent.skills.log_analyze import validate_citations
    ok, _ = validate_citations("服务启动失败，原因不明", valid_max_line=20)
    assert ok is False


class _FakeLLM:
    def __init__(self, content):
        self.content = content

    def invoke(self, _msgs):
        return self


def _fake_chat_factory(content):
    def _fake(*_a, **_k):
        return _FakeLLM(content)
    return _fake


def _seed_app_logs(conn, tid: int) -> None:
    from test_agent.skills.app_logs import parse_app_log, store_app_logs
    entries = parse_app_log(PY_LOG, "python")
    store_app_logs(conn, tid, 1, entries, source="file",
                   log_path="data/workspaces/task_x/logs/app_r1.log", log_lines=7)


def test_run_log_analyze_rejects_fabricated_line(tmp_path, monkeypatch):
    """LLM 叙述引用不存在的行号 → 必须失败（闸门负例，任务卡点名单例）。"""
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    tid = _seed_task(conn)
    _seed_app_logs(conn, tid)
    from test_agent.skills import log_analyze as la
    bad = ('{"summary": "端口被占用导致启动失败，见 行 999 的绑定错误", '
           '"items": [{"line": 999, "explain": "address already in use"}]}')
    monkeypatch.setattr(la, "make_chat", _fake_chat_factory(bad))
    out = la.run_log_analyze("sk-fake", conn, tid, round_no=1)
    assert out.get("citations_ok") is False
    assert "行号" in out.get("error", "")
    assert "999" in out.get("error", "")


def test_run_log_analyze_accepts_real_line(tmp_path, monkeypatch):
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    tid = _seed_task(conn)
    _seed_app_logs(conn, tid)
    from test_agent.skills import log_analyze as la
    good = ('{"summary": "行 3 为端口绑定失败（address already in use），'
            '行 6 的 RuntimeError 是其连锁异常", '
            '"items": [{"line": 3, "explain": "端口占用"}, {"line": 6, "explain": "连锁异常"}]}')
    monkeypatch.setattr(la, "make_chat", _fake_chat_factory(good))
    out = la.run_log_analyze("sk-fake", conn, tid, round_no=1)
    assert out.get("citations_ok") is True
    assert "端口" in out.get("summary", "")
    assert len(out.get("items", [])) == 2


def test_run_log_analyze_skips_when_no_logs(tmp_path):
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    tid = _seed_task(conn)
    from test_agent.skills import log_analyze as la
    out = la.run_log_analyze("sk-fake", conn, tid)
    assert "skipped" in out


# ---------- 3) 日志分流路径拼接 ----------

def test_host_snapshot_path_naming():
    from test_agent.skills.app_logs import host_snapshot_path
    ws = Path("data/workspaces/task_42")
    assert host_snapshot_path(ws, 1) == ws / "logs" / "app_r1.log"
    assert host_snapshot_path(ws, 3).name == "app_r3.log"


def test_container_log_path_constant():
    from test_agent.skills.app_logs import APP_LOG_CONTAINER_PATH
    assert APP_LOG_CONTAINER_PATH == "/work/logs/app.log"


def test_choose_log_source_fallback():
    from test_agent.skills.app_logs import choose_log_source
    assert choose_log_source("file-text", "docker") == ("file-text", "file")
    assert choose_log_source("", "docker-tail") == ("docker-tail", "docker_logs")
    assert choose_log_source("", "") == ("", "empty")


# ---------- 4) app_logs 落库幂等 ----------

def test_store_app_logs_idempotent_same_round(tmp_path):
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    tid = _seed_task(conn)
    from test_agent.skills.app_logs import parse_app_log, store_app_logs
    entries = parse_app_log(PY_LOG, "python")
    n1 = store_app_logs(conn, tid, 1, entries, source="file",
                        log_path="x", log_lines=7)
    n2 = store_app_logs(conn, tid, 1, entries, source="file",
                        log_path="x", log_lines=7)
    cnt = conn.execute("SELECT COUNT(*) c FROM app_logs WHERE task_id=?", (tid,)).fetchone()["c"]
    assert n1 == n2 == cnt and cnt > 0        # 重存同轮不重复计行


def test_store_app_logs_rerun_from_round1_clears_history(tmp_path):
    """任务重跑（round 从 1 再起）以本轮为准，历史轮清空——与 evaluation_rounds 同语义。"""
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    tid = _seed_task(conn)
    from test_agent.skills.app_logs import parse_app_log, store_app_logs
    entries = parse_app_log(PY_LOG, "python")
    store_app_logs(conn, tid, 1, entries, source="file", log_path="x", log_lines=7)
    store_app_logs(conn, tid, 2, entries, source="file", log_path="x", log_lines=7)
    rounds = {r["round_no"] for r in conn.execute(
        "SELECT DISTINCT round_no FROM app_logs WHERE task_id=?", (tid,))}
    assert rounds == {1, 2}
    store_app_logs(conn, tid, 1, entries, source="file", log_path="x", log_lines=7)
    rounds = {r["round_no"] for r in conn.execute(
        "SELECT DISTINCT round_no FROM app_logs WHERE task_id=?", (tid,))}
    assert rounds == {1}


# ---------- 表契约（28 表，含 app_logs） ----------

def test_app_logs_table_in_schema(tmp_path):
    assert db.init_db(tmp_path / "t.db") == 34
    conn = db.get_conn(tmp_path / "t.db")
    n = conn.execute("SELECT COUNT(*) c FROM sqlite_master WHERE name='app_logs'").fetchone()["c"]
    assert n == 1


def test_log_analyze_skill_registered():
    """集成层：log_analyze 必须经 build_skills 注册（schema/风险分级随 Skill 元数据落库）。"""
    from test_agent.skills.builtin import build_skills
    skills = build_skills(runner=None)
    sk = skills["log_analyze"]
    assert sk.risk_level == "low"
    assert "round_no" in sk.arg_schema
    assert sk.timeout_ms >= 120_000        # LLM 调用留足余量（defect_analysis 同档）
