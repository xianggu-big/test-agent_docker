"""第三轮修复锁（2026-11-15 闲时找bug 第三轮，R11 SQL 写路径完备性 +
R12 旗标/正则边界维度）。

实锤四处：
1. api_add_case 的 imported 宿主脚本按 version DESC 取——任务有生成/反馈脚本时
   几乎必挂错行；toggle 重建 test_imported 内容回写该错行 → 生成脚本行 content
   被整体替换 → 阶段二布考场按库重写文件=fb 用例从考场消失（数据污染链）。
2. llm_profile.apply_routing 裸 UPDATE 无 INSERT 分支——全新库（app_settings 无
   llm_profile 行）PUT /api/llm-routing 成功返回但静默丢失，GET 回读 routing={}。
3. repair_strategy 重写脚本落库不带 filename 且无 delete-first——重跑同轮 rewrite
   时行翻倍（评审页用例清单翻倍），filename 列空使一切按名幂等机制失效。
4. parse_scenarios/parse_acceptance/_parse_cases 的 re.search 缺 None 保护——
   LLM content=None（备胎/异常 provider 形态）时 TypeError 取代"输出不可解析"
   诚实降级。
"""
import pytest
from fastapi.testclient import TestClient

from test_agent import config


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.queue import QueueWorker
    monkeypatch.setattr(QueueWorker, "start", lambda self: None)
    from test_agent.api import server
    with TestClient(server.app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        from test_agent.store import db
        c = db.get_conn()
        uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                        (uid,)).lastrowid
        tid = c.execute(
            "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
            "VALUES(?,?,0.7,'ready_for_review',1)", (pid, uid)).lastrowid
        # 阶段一产物：生成脚本行（fb 版本 101 最高）+ 对应用例
        gen_sid = c.execute(
            "INSERT INTO test_scripts(task_id,module_id,framework,version,filename,"
            "content,content_hash) VALUES(?,NULL,'pytest',101,'test_gen_fb1.py',"
            "'GEN_CONTENT','h')", (tid,)).lastrowid
        c.execute("INSERT INTO test_cases(script_id,source,name,method,content) "
                  "VALUES(?, 'ai', 'test_fb_case', '等价类', 'def test_fb_case(): pass')",
                  (gen_sid,))
        c.commit()
        c.close()
        (tmp_path / "workspaces" / f"task_{tid}").mkdir(parents=True)
        yield client, h, tid, gen_sid, tmp_path


# ---------- 1. imported 宿主挂错行 → toggle 污染生成脚本 ----------

def test_add_case_attaches_to_imported_host_not_gen_script(env):
    """任务已有生成脚本（version 最高）时插入 imported 用例——必须挂到独立的
    imported 宿主行，不能按 version DESC 挂到生成脚本行。"""
    client, h, tid, gen_sid, _ = env
    r = client.post(f"/api/tasks/{tid}/cases", headers=h,
                    json={"name": "test_manual", "method": "人工评审补充",
                          "code": "def test_manual():\n    assert 1 + 1 == 2\n"})
    assert r.status_code == 200, r.text
    from test_agent.store import db
    c = db.get_conn()
    row = c.execute(
        "SELECT script_id FROM test_cases WHERE source='imported' AND name='test_manual'"
    ).fetchone()
    c.close()
    assert row is not None and row["script_id"] != gen_sid, \
        "imported 用例挂到生成脚本行——toggle 重建会整体替换该行 content（污染链头）"


def test_toggle_imported_does_not_overwrite_gen_script(env):
    """toggle imported 用例只改 imported 宿主行——生成脚本行 content 必须原样。"""
    client, h, tid, gen_sid, _ = env
    r = client.post(f"/api/tasks/{tid}/cases", headers=h,
                    json={"name": "test_manual", "method": "人工",
                          "code": "def test_manual():\n    assert 1\n"})
    assert r.status_code == 200
    cid = client.get(f"/api/tasks/{tid}/cases?all=1", headers=h).json()["cases"]
    cid = next(x["id"] for x in cid if x["name"] == "test_manual")
    r = client.post(f"/api/cases/{cid}/toggle", headers=h)
    assert r.status_code == 200, r.text
    from test_agent.store import db
    c = db.get_conn()
    content = c.execute("SELECT content FROM test_scripts WHERE id=?",
                        (gen_sid,)).fetchone()["content"]
    c.close()
    assert content == "GEN_CONTENT", "生成脚本行 content 被 imported 重建内容整体替换"


# ---------- 2. apply_routing 全新库静默丢失 ----------

def test_apply_routing_on_fresh_db_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    c = db.get_conn()
    from test_agent.skills.llm_profile import apply_routing
    doc = apply_routing({}, c, "tester")            # 空路由也是合法写（清空语义）
    row = c.execute("SELECT value FROM app_settings WHERE key='llm_profile'").fetchone()
    c.close()
    assert row is not None, "全新库裸 UPDATE 命中 0 行——PUT 成功但路由静默丢失"
    assert doc is not None


# ---------- 3. rewrite 落库带 filename + delete-first（结构锁） ----------

def test_rewrite_insert_has_filename_and_delete_first():
    src = (config.__file__.rsplit("config", 1)[0] + "skills" + "\\repair_strategy.py")
    from pathlib import Path
    p = Path(config.__file__).parent / "skills" / "repair_strategy.py"
    s = p.read_text(encoding="utf-8")
    assert "test_rewrite_r{round_no}.py" in s
    # 落库必须带 filename 列 + 前置 delete-first（与 gen_tests 同款重跑幂等）
    assert "DELETE FROM test_cases WHERE script_id IN" in s
    assert s.index("DELETE FROM test_cases WHERE script_id IN") < \
        s.index("INSERT INTO test_scripts(task_id, framework, version")
    ins = s[s.index("INSERT INTO test_scripts(task_id, framework, version"):
            s.index("VALUES (?, 'pytest', ?", s.index("INSERT INTO test_scripts(task_id")) + 40]
    assert "filename" in ins, "rewrite 落库缺 filename 列——按名幂等机制全部失效"


# ---------- 4. 解析器 None 容错（content=None 诚实降级不 TypeError） ----------

def test_parse_scenarios_none_content_degrades():
    from test_agent.skills.scenario_gen import parse_acceptance, parse_scenarios
    assert parse_scenarios(None, []) == ([], ["LLM 输出不可解析"])
    assert parse_acceptance(None, []) == ([], ["LLM 输出不可解析"])


def test_parse_cases_none_content_degrades():
    from test_agent.skills.gen_tests import _parse_cases
    assert _parse_cases(None) is None
