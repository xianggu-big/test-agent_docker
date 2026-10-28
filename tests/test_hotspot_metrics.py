"""W4 复杂度热点指标全链路：analyze 落库 → hotspot_coverage 交叉求值 →
指标包 / 降级叙述 / 规则闸 / 报告 API。

设计文档 §3-W4 ②「报告新增指标：复杂度热点未覆盖」的落地回归：
- 覆盖状态不回写表，读取时由 per_file 缺口行确定性求值（只认正向证据）
- 老任务（无热点行 / per_file 为 NULL 的老报告）三字段整体不出现，叙述不得编造
"""
import json

import pytest

from test_agent import config
from test_agent.skills.evaluate import (_missing_lines, collect_metrics,
                                        fallback_report, hotspot_coverage,
                                        review_rules)
from test_agent.store import db


# ---------------------------------------------------------------------------
# 纯函数：term-missing 行集展开 与 热点×覆盖交叉
# ---------------------------------------------------------------------------

class TestMissingLines:
    def test_单行与区间混合(self):
        assert _missing_lines("10-12, 30,7") == {10, 11, 12, 30, 7}

    def test_空与乱格式容错(self):
        assert _missing_lines("") == set()
        assert _missing_lines("x-2, 8-, -") == set()   # 非数字端点整体丢弃


class TestHotspotCoverage:
    HS = [{"file": "cart.py", "name": "hot", "complexity": 12, "lineno": 11},
          {"file": "cart.py", "name": "other", "complexity": 10, "lineno": 5},
          {"file": "other.py", "name": "f", "complexity": 20, "lineno": 99}]

    def test_三分支_命中missing_在列不命中_文件不在列(self):
        per_file = [{"file": "cart.py", "stmts": 20, "miss": 5, "cover": 75,
                     "missing": "10-12, 30"}]
        r = hotspot_coverage(self.HS, per_file)
        assert r["total"] == 3 and r["uncovered"] == 1
        assert r["uncovered_list"] == ["cart.py::hot(V(G)=12)"]

    def test_文件不在per_file即整文件覆盖(self):
        """per_file 只收有缺口的文件（B2）——未列出=无缺口证据=覆盖。"""
        r = hotspot_coverage(self.HS, [])
        assert r == {"total": 3, "uncovered": 0, "uncovered_list": []}

    def test_空热点与空per_file(self):
        assert hotspot_coverage([], [{"file": "a.py", "missing": "1"}]
                                )["total"] == 0


# ---------------------------------------------------------------------------
# collect_metrics 集成：热点指标进包 + 老任务兼容
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
    yield c, tid
    c.close()


def _add_report(c, tid, per_file):
    c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped,coverage_line,per_file)
                 VALUES(?,?,?,? ,0.85,?)""", (tid, 8, 1, 1, per_file))
    c.commit()


class TestCollectHotspotMetrics:
    def test_热点指标进包(self, conn):
        c, tid = conn
        _add_report(c, tid, json.dumps(
            [{"file": "cart.py", "stmts": 20, "miss": 5, "cover": 75, "missing": "10-12"}]))
        for h in (("cart.py", "hot", 12, "C", 11), ("cart.py", "other", 10, "C", 5),
                  ("other.py", "f", 20, "D", 99)):
            c.execute("""INSERT INTO complexity_hotspots(task_id,file,name,complexity,rank,lineno)
                         VALUES(?,?,?,?,?,?)""", (tid, *h))
        c.commit()
        m = collect_metrics(c, tid)
        assert m["hotspots_total"] == 3 and m["hotspots_uncovered"] == 1
        assert m["hotspots_uncovered_list"] == ["cart.py::hot(V(G)=12)"]

    def test_老报告per_file为NULL_全部按覆盖计(self, conn):
        """B2 之前的报告行 per_file 为 NULL——无缺口证据即覆盖，指标如实 0/N。"""
        c, tid = conn
        _add_report(c, tid, None)
        c.execute("""INSERT INTO complexity_hotspots(task_id,file,name,complexity,rank,lineno)
                     VALUES(?,?,?,?,?,?)""", (tid, "a.py", "f", 11, "C", 3))
        c.commit()
        m = collect_metrics(c, tid)
        assert m["hotspots_total"] == 1 and m["hotspots_uncovered"] == 0

    def test_无热点行_三字段不出现(self, conn):
        """老任务（建表前实验数据 / 非 python 链路）无热点行：字段整体缺席，
        叙述层只能写"无数据"，不得默认成 0/N。"""
        c, tid = conn
        _add_report(c, tid, json.dumps(
            [{"file": "cart.py", "stmts": 20, "miss": 5, "cover": 75, "missing": "1-99"}]))
        m = collect_metrics(c, tid)
        assert "hotspots_total" not in m and "hotspots_uncovered" not in m


# ---------------------------------------------------------------------------
# 叙述与规则闸：固定表述 + 出现即比对
# ---------------------------------------------------------------------------

class TestNarrativeAndGate:
    M = {"task_id": 1, "passed": 8, "failed": 1, "skipped": 1, "pass_rate": 0.8,
         "coverage_line": 0.85, "coverage_target": 0.7, "task_status": "done",
         "stop_reason": None, "failure_kind": None,
         "hotspots_total": 5, "hotspots_uncovered": 2,
         "hotspots_uncovered_list": ["a.py::f(V(G)=12)", "b.py::g(V(G)=14)"]}

    def test_降级报告含固定表述(self):
        text = fallback_report(self.M, {"matrix": []})
        assert "复杂度热点未覆盖 2/5" in text
        assert "a.py::f(V(G)=12)" in text

    def test_降级报告无热点指标不造行(self):
        m = {k: v for k, v in self.M.items() if not k.startswith("hotspots")}
        assert "热点未覆盖" not in fallback_report(m, {"matrix": []})

    def test_规则闸_数字不符reject(self):
        reasons = review_rules("复杂度热点未覆盖 3/5", self.M, "pass")
        assert any("热点未覆盖" in r and "3/5" in r for r in reasons)

    def test_规则闸_数字一致过(self):
        assert not review_rules("复杂度热点未覆盖 2/5", self.M, "pass")

    def test_规则闸_无热点指标时提及不校验(self):
        m = {k: v for k, v in self.M.items() if not k.startswith("hotspots")}
        assert not review_rules("复杂度热点未覆盖 2/5", m, "pass")


# ---------------------------------------------------------------------------
# analyze 节点落库：热点快照 delete-first 幂等
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from test_agent.graph.nodes import GraphDeps, make_nodes
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status) "
                    "VALUES(?,?,0.7,'queued')", (pid, uid)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    deps = GraphDeps(conn=c, runner=type("R", (), {})(), registry=None, skills={})
    yield c, make_nodes(deps), tid, ses, tmp_path
    c.close()


# 11 个独立 if → V(G)=12（rank C，稳过热点线，不踩 radon 阈值边界）
_COMPLEX_SRC = "def hot(x):\n    c = 0\n" + \
    "".join(f"    if x == {i}:\n        c += {i}\n" for i in range(11)) + \
    "    return c\n"


class TestAnalyzePersists:
    def test_热点落库_字段与表一致(self, env):
        c, nodes, tid, ses, tmp = env
        ws = tmp / "ws_hot"
        ws.mkdir()
        (ws / "hot.py").write_text(_COMPLEX_SRC, encoding="utf-8")
        out = nodes["analyze"]({"task_id": tid, "session_id": ses, "workspace": str(ws)})
        rows = c.execute("SELECT file, name, complexity, rank, lineno FROM"
                         " complexity_hotspots WHERE task_id=?", (tid,)).fetchall()
        assert [(r["file"], r["name"]) for r in rows] == [("hot.py", "hot")]
        assert rows[0]["rank"] == "C"
        assert out["complexity_hotspots"][0]["complexity"] == rows[0]["complexity"]

    def test_重跑delete_first不残留(self, env):
        """第二轮扫不到热点（代码全低复杂度）→ 旧热点行整体清空，指标不残留失真。"""
        c, nodes, tid, ses, tmp = env
        ws1 = tmp / "ws_a"
        ws1.mkdir()
        (ws1 / "hot.py").write_text(_COMPLEX_SRC, encoding="utf-8")
        nodes["analyze"]({"task_id": tid, "session_id": ses, "workspace": str(ws1)})
        assert c.execute("SELECT COUNT(*) FROM complexity_hotspots WHERE task_id=?",
                         (tid,)).fetchone()[0] == 1

        ws2 = tmp / "ws_b"
        ws2.mkdir()
        (ws2 / "simple.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        nodes["analyze"]({"task_id": tid, "session_id": ses, "workspace": str(ws2)})
        assert c.execute("SELECT COUNT(*) FROM complexity_hotspots WHERE task_id=?",
                         (tid,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 报告 API：hotspots 字段随报告行返回（前端指标条第 5 格数据源）
# ---------------------------------------------------------------------------

class TestApiReportHotspots:
    @pytest.fixture()
    def api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
        from fastapi.testclient import TestClient
        from test_agent.api import server
        with TestClient(server.app) as client:
            r = client.post("/api/login", json={"username": "admin",
                                                "password": "admin123"})
            h = {"Authorization": f"Bearer {r.json()['token']}"}
            c = db.get_conn()
            uid = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            pid = c.execute("INSERT INTO projects(user_id,repo_url,name)"
                            " VALUES(?,'r','n')", (uid,)).lastrowid
            t1 = c.execute("INSERT INTO test_tasks(project_id,user_id,status) "
                           "VALUES(?,?,'done')", (pid, uid)).lastrowid
            t2 = c.execute("INSERT INTO test_tasks(project_id,user_id,status) "
                           "VALUES(?,?,'done')", (pid, uid)).lastrowid
            c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped,per_file)
                         VALUES(?,?,?,?,?)""",
                      (t1, 8, 1, 1, json.dumps(
                          [{"file": "cart.py", "stmts": 20, "miss": 5, "cover": 75,
                            "missing": "10-12"}])))
            c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped,per_file)
                         VALUES(?,?,?,?,?)""", (t2, 3, 0, 0, None))
            for h5 in (("cart.py", "hot", 12, "C", 11), ("other.py", "g", 14, "C", 40)):
                c.execute("""INSERT INTO complexity_hotspots(task_id,file,name,
                                                              complexity,rank,lineno)
                             VALUES(?,?,?,?,?,?)""", (t1, *h5))
            c.commit()
            c.close()
            yield client, h, t1, t2

    def test_有热点任务_字段与交叉值(self, api):
        client, h, t1, _ = api
        r = client.get(f"/api/tasks/{t1}/report", headers=h)
        assert r.status_code == 200
        assert r.json()[0]["hotspots"] == {"total": 2, "uncovered": 1}

    def test_无热点任务_无字段(self, api):
        client, h, _, t2 = api
        r = client.get(f"/api/tasks/{t2}/report", headers=h)
        assert r.status_code == 200 and "hotspots" not in r.json()[0]

    def test_未鉴权401(self, api):
        client, _, t1, _ = api
        assert client.get(f"/api/tasks/{t1}/report").status_code == 401
