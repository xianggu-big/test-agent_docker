"""第二轮修复锁（2026-11-15 闲时找bug 第二轮，R10 新维度：前端消费面对账 +
降级链完备性 + 备胎构造对齐）。

实锤五处：
1. /api/tasks/{tid}/levels 不返回 task_id → 详情页四级卡三个动作按钮（查看报告/
   确认场景/L4 转化）全部落到任务 0（viewReport(0) 静默无操作、其余 404）；
2. /api/dashboard 的 running/pending_review/feed 无 progress 键 → 执行卡恒显示 0%
   （/api/tasks 与 /page 都注入 progress，dashboard 漏注入）；
3. probe 降级链 proto 档排在 go 档之前且产物形状异形（proto/service/rpc，无
   method/path）→ Go+proto 并存项目被 proto 截胡，l2 extract e["method"] 抛
   KeyError 被 registry 吞成 failed，L2 整级 skipped 且报错误导向 no_spec；
4. api_verify_fix 创建验证任务但不准备工作区（prepare_verify_workspace 只有离线
   脚本调用）→ 按提示下发阶段二必失败"工作区不存在"——UI 通道回归验证闭环结构性断；
5. RobustChat._try_fallback 构造备胎绕过 _apply_local_think_off（思考型备胎
   content 恒空——模块自证坑）与 utf-8 http_client（CDN gbk mojibake 修复未覆盖
   备胎），且 temperature 恒 0.2（invoke 期 kwargs 不含构造参数）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)   # 防真 worker 干扰
    from test_agent.api import server, auth
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        auth.register("r2_caller", "caller123")
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
            "VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid
        c.commit()
        c.close()
        yield client, h, tid, tmp_path, (uid, pid)


# ---------- 1. levels 返回 task_id ----------

def test_levels_rows_include_task_id(env):
    client, h, tid, _, _ = env
    from test_agent.store import db
    c = db.get_conn()
    c.execute("INSERT INTO test_levels(task_id, level, strategy, status) "
              "VALUES(?, 'L1', 'full', 'done')", (tid,))
    c.commit()
    c.close()
    levels = client.get(f"/api/tasks/{tid}/levels", headers=h).json()["levels"]
    assert levels and levels[0].get("task_id") == tid, \
        "四级卡动作按钮靠 l.task_id 跳转——缺键=全部落到任务 0"


def test_levels_synthetic_card_includes_task_id(env):
    """无级别行的直接 L1 任务：合成回退卡也要带 task_id（同一消费方）。"""
    client, h, tid, _, _ = env
    from test_agent.store import db
    c = db.get_conn()
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,content,content_hash) "
                    "VALUES(?, 'pytest', 1, 'x', 'h')", (tid,)).lastrowid
    c.execute("INSERT INTO test_cases(script_id,source,name,content) "
              "VALUES(?, 'ai', 't', 'x')", (sid,))
    c.execute("INSERT INTO evaluation_rounds(task_id,level,round_no,passed,failed,skipped,"
              "verdict) VALUES(?, 'L1', 1, 1, 0, 0, 'pass')", (tid,))
    c.commit()
    c.close()
    levels = client.get(f"/api/tasks/{tid}/levels", headers=h).json()["levels"]
    assert levels and levels[0].get("task_id") == tid


# ---------- 2. dashboard running/pending/feed 带 progress ----------

def test_dashboard_running_rows_include_progress(env):
    client, h, tid, _, _ = env
    from test_agent.store import db
    c = db.get_conn()
    c.execute("UPDATE test_tasks SET status='running' WHERE id=?", (tid,))
    c.commit()
    c.close()
    d = client.get("/api/dashboard", headers=h).json()
    row = next((r for r in d["running"] if r["id"] == tid), None)
    assert row is not None
    assert row.get("progress") == 70, "执行卡进度恒 0%——dashboard 漏注入 progress（/tasks 已注入）"


# ---------- 3. probe 链 proto 不截胡 go ----------

def test_probe_prefers_go_over_proto_for_http_track(tmp_path):
    """Go+proto 并存：L2 的 HTTP 端点提取必须落到 go 档（gin 路由可渲染契约），
    不能被 proto 档截胡（异形产物进 l2 extract 即 KeyError 被吞→L2 假 no_spec）。"""
    from test_agent.skills.endpoint_extract import probe_endpoint_source
    (tmp_path / "svc.proto").write_text(
        'syntax = "proto3";\nservice Greeter { rpc SayHello (Req) returns (Res); }\n'
        "message Req { string name = 1; }\nmessage Res { string message = 1; }\n",
        encoding="utf-8")
    (tmp_path / "main.go").write_text(
        'package main\nimport "github.com/gin-gonic/gin"\n'
        'func main() {\n  r := gin.Default()\n  r.GET("/ping", h)\n  r.Run(":8080")\n}\n',
        encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert eps and eps[0].get("method") == "GET", \
        f"proto 档截胡（how={how}）——HTTP 轨应先试 go 提取"


# ---------- 4. verify-fix 端点准备验证工作区 ----------

def test_verify_fix_endpoint_prepares_workspace(env, tmp_path):
    """本地目录项目：端点创建验证任务后必须同轮准备快照工作区——否则按提示下发
    阶段二必失败"工作区不存在"（prepare 此前只有离线脚本调用）。"""
    client, h, tid, _, (uid, _pid) = env
    src = tmp_path / "fixed_repo"
    src.mkdir()
    (src / "app.py").write_text("def add(a, b):\n    return a + b  # 修复后\n",
                                encoding="utf-8")
    from test_agent.store import db
    c = db.get_conn()
    c.execute("UPDATE projects SET repo_url=? WHERE id="
              "(SELECT project_id FROM test_tasks WHERE id=?)", (str(src), tid))
    c.commit()
    c.close()
    r = client.post(f"/api/tasks/{tid}/verify-fix", headers=h)
    assert r.status_code == 200, r.text
    vid = r.json()["verify_task_id"]
    ws = tmp_path / "workspaces" / f"task_{vid}"
    assert (ws / "app.py").exists(), "验证任务工作区未准备——阶段二下发必失败"


def test_verify_fix_remote_repo_honest_409(env, tmp_path):
    """git URL 项目（v1 不支持 checkout 修复 commit）→ 诚实 409，不造必失败的任务。"""
    client, h, tid, _, _ = env
    from test_agent.store import db
    c = db.get_conn()
    c.execute("UPDATE projects SET repo_url=? WHERE id="
              "(SELECT project_id FROM test_tasks WHERE id=?)",
              ("https://github.com/x/y.git", tid))
    c.commit()
    c.close()
    r = client.post(f"/api/tasks/{tid}/verify-fix", headers=h)
    assert r.status_code == 409 and "本地目录" in r.json()["detail"]


# ---------- 5. 备胎构造对齐主路径 ----------

def test_fallback_construction_parity(monkeypatch):
    """备胎构造必须与主路径同款：思考型模型关思考（否则 content 恒空——模块自证）、
    utf-8 http_client（CDN gbk mojibake 修复）、temperature 透传（非恒 0.2）。"""
    from test_agent.skills import llm_client
    captured = {}

    class _FakeChat:
        def __init__(self, **kw):
            captured.update(kw)
            self.temperature = kw.get("temperature", 0.2)

        def invoke(self, messages, *a, **kw):
            class _R:
                content = "ok"
                additional_kwargs = {}
            return _R()

    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChat)   # 备胎是函数内局部 import
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "qwen3.5-somemodel")
    monkeypatch.setattr(config, "LLM_FALLBACK_BASE_URL", "http://localhost:11434/v1")

    class _Inner:
        temperature = 0.1

    rc = llm_client.RobustChat(_Inner(), retries=1, phase="plan", task_id=None)
    resp = rc._try_fallback([("user", "hi")], reason="primary_unreachable")
    assert resp is not None
    assert captured.get("temperature") == 0.1, "temperature 必须透传原构造值（非恒 0.2）"
    mk = captured.get("model_kwargs") or {}
    assert mk.get("reasoning_effort") == "none", "思考型备胎必须关思考（content 恒空坑）"
    assert captured.get("http_client") is not None, "必须带 utf-8 http_client（mojibake 修复）"
