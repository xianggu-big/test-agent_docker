"""P1-E LLM 调用成本台账全链路：usage 提取 → llm_calls 落库 →
collect_metrics 聚合 / 降级叙述 / 规则闸 / 报告 API。

复盘文档 [P1-E] 的落地回归：
- usage 只认 langchain response.usage_metadata；缺失（fake-LLM / 降级链路）不落行，
  诚实不记 0
- 老任务（建表前实验数据 / 全程无 Key 的确定性链路）无行 → 四字段整体不出现，
  叙述不得编造
- 记账失败（FK 违例等）吞掉不上抛——成本台账绝不炸主链路
"""
import json
from types import SimpleNamespace

import pytest

from test_agent import config
from test_agent.skills.evaluate import collect_metrics, fallback_report, review_rules
from test_agent.skills.llm_usage import extract_usage, log_llm_call
from test_agent.store import db


def _resp(p=100, c=50, t=150, omit_total=False):
    um = {"input_tokens": p, "output_tokens": c}
    if not omit_total:
        um["total_tokens"] = t
    return SimpleNamespace(content="x", usage_metadata=um)


# ---------------------------------------------------------------------------
# 纯函数：usage 提取
# ---------------------------------------------------------------------------

class TestExtractUsage:
    def test_标准三元组(self):
        assert extract_usage(_resp()) == (100, 50, 150)

    def test_total缺失按和补(self):
        assert extract_usage(_resp(100, 50, 0, omit_total=True)) == (100, 50, 150)

    def test_无usage_metadata属性(self):
        """fake-LLM 单测注入的 resp（只有 content）→ None，不落行不记 0。"""
        assert extract_usage(SimpleNamespace(content="x")) is None

    def test_空与非数字与全零(self):
        assert extract_usage(SimpleNamespace(usage_metadata={})) is None
        assert extract_usage(
            SimpleNamespace(usage_metadata={"input_tokens": "a",
                                            "output_tokens": 5})) is None
        assert extract_usage(_resp(0, 0, 0)) is None


# ---------------------------------------------------------------------------
# 落库：自建短连接 + 异常吞噬
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status) "
                    "VALUES(?,?,0.7,'done')", (pid, uid)).lastrowid
    c.commit()   # 释放 seed 事务：log_llm_call 自建连接写库，撞未提交事务会 locked
    yield c, tid
    c.close()


def _calls(c, tid):
    return c.execute("SELECT * FROM llm_calls WHERE task_id=?", (tid,)).fetchall()


class TestLogLlmCall:
    def test_正常落行_字段全对(self, conn):
        c, tid = conn
        log_llm_call(tid, "plan", _resp())
        rows = _calls(c, tid)
        assert len(rows) == 1
        r = rows[0]
        assert (r["phase"], r["prompt_tokens"], r["completion_tokens"],
                r["total_tokens"]) == ("plan", 100, 50, 150)
        assert r["model"] == config.DEEPSEEK_MODEL

    def test_无usage不落行(self, conn):
        c, tid = conn
        log_llm_call(tid, "gen_tests", SimpleNamespace(content="x"))
        assert _calls(c, tid) == []

    def test_task_id为None跳过(self, conn):
        c, tid = conn
        log_llm_call(None, "refill", _resp())
        assert _calls(c, tid) == []

    def test_FK违例不上抛(self, conn):
        """记账失败绝不炸主链路（吞掉只 warn）。"""
        c, tid = conn
        log_llm_call(999999, "rewrite", _resp())
        assert _calls(c, tid) == []


# ---------------------------------------------------------------------------
# collect_metrics 聚合：四字段进包 + 老任务兼容
# ---------------------------------------------------------------------------

def _add_report(c, tid):
    c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped,coverage_line)
                 VALUES(?,?,?,? ,0.85)""", (tid, 8, 1, 1))
    c.commit()


class TestCollectMetrics:
    def test_多行聚合_跨phase累积(self, conn):
        c, tid = conn
        _add_report(c, tid)
        log_llm_call(tid, "plan", _resp(100, 50, 150))
        log_llm_call(tid, "gen_tests", _resp(200, 100, 300))
        m = collect_metrics(c, tid)
        assert m["llm_call_count"] == 2
        assert m["llm_prompt_tokens"] == 300
        assert m["llm_completion_tokens"] == 150
        assert m["llm_total_tokens"] == 450

    def test_无行_四字段整体不出现(self, conn):
        """老任务（建表前实验数据 / 全程无 Key 链路）：字段整体缺席，
        叙述层只能写"无 LLM 成本数据"，不得默认成 0。"""
        c, tid = conn
        _add_report(c, tid)
        m = collect_metrics(c, tid)
        assert not any(k.startswith("llm_") for k in m)


# ---------------------------------------------------------------------------
# 叙述与规则闸：固定表述 + 出现即比对
# ---------------------------------------------------------------------------

class TestNarrativeAndGate:
    M = {"task_id": 1, "passed": 8, "failed": 1, "skipped": 1, "pass_rate": 0.8,
         "coverage_line": 0.85, "coverage_target": 0.7, "task_status": "done",
         "stop_reason": None, "failure_kind": None,
         "llm_call_count": 3, "llm_prompt_tokens": 100,
         "llm_completion_tokens": 50, "llm_total_tokens": 150}

    def test_降级报告含固定表述(self):
        text = fallback_report(self.M, {"matrix": []})
        assert "LLM 调用 3 次、token 消耗 100/50/150" in text

    def test_降级报告无成本指标不造行(self):
        m = {k: v for k, v in self.M.items() if not k.startswith("llm_")}
        assert "token 消耗" not in fallback_report(m, {"matrix": []})

    def test_规则闸_数字不符reject(self):
        reasons = review_rules("token 消耗 1/2/3（输入/输出/合计）", self.M, "pass")
        assert any("token 消耗" in r and "1/2/3" in r for r in reasons)

    def test_规则闸_数字一致过(self):
        assert not review_rules("token 消耗 100/50/150（输入/输出/合计）",
                                self.M, "pass")

    def test_规则闸_无成本指标时提及不校验(self):
        m = {k: v for k, v in self.M.items() if not k.startswith("llm_")}
        assert not review_rules("token 消耗 1/2/3", m, "pass")


# ---------------------------------------------------------------------------
# 报告 API：llm 字段随报告行返回（前端指标条第 6 格数据源）
# ---------------------------------------------------------------------------

class TestApiReportLlm:
    @pytest.fixture()
    def api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
        from fastapi.testclient import TestClient
        from test_agent.api import server
        with TestClient(server.app) as client:
            r = client.post("/api/login", json={"username": "admin",
                                                "password": "admin123"})
            assert r.status_code == 200, f"login 失败: {r.status_code} {r.text[:200]}"
            h = {"Authorization": f"Bearer {r.json()['token']}"}
            c = db.get_conn()
            uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            pid = c.execute("INSERT INTO projects(user_id,repo_url,name)"
                            " VALUES(?,'r','n')", (uid,)).lastrowid
            t1 = c.execute("INSERT INTO test_tasks(project_id,user_id,status) "
                           "VALUES(?,?,'done')", (pid, uid)).lastrowid
            t2 = c.execute("INSERT INTO test_tasks(project_id,user_id,status) "
                           "VALUES(?,?,'done')", (pid, uid)).lastrowid
            for t in (t1, t2):
                c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped)
                             VALUES(?,?,?,?)""", (t, 8, 1, 1))
            c.commit()   # 先释放事务，log_llm_call 的新连接才能写入
            log_llm_call(t1, "plan", _resp(100, 50, 150))
            log_llm_call(t1, "gen_tests", _resp(200, 100, 300))
            c.close()
            yield client, h, t1, t2

    def test_有台账任务_llm字段为任务级累积(self, api):
        client, h, t1, _ = api
        r = client.get(f"/api/tasks/{t1}/report", headers=h)
        assert r.status_code == 200
        assert r.json()[0]["llm"] == {"calls": 2, "total": 450}

    def test_无台账任务_无llm字段(self, api):
        client, h, _, t2 = api
        r = client.get(f"/api/tasks/{t2}/report", headers=h)
        assert r.status_code == 200
        assert "llm" not in r.json()[0]
