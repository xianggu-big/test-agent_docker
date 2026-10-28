"""评估五工具单测：指标包确定性 / RTM 启发式映射 / 审查规则闸 / 降级报告自洽 —— 全离线。

LLM 通道（map_requirements 的 llm 映射、gen_report 叙述、review 的 LLM 闸）依赖网络，
机制由本文件的确定性通道单测锁死；LLM 路径的鲁棒性由"解析失败即降级"的 except 分支保证。
"""
import json

import pytest

from test_agent import config
from test_agent.skills.evaluate import (build_eval_skills, collect_metrics,
                                        fallback_report, map_requirements,
                                        review_rules)
from test_agent.store import db


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """真库（init_db 全 schema）+ 一套完整任务数据：报告/轨迹/调用台账/用例/条件。

    skill 包装层在线程池内自建连接（sqlite3 禁止跨线程复用），统一走 config.DB_PATH，
    所以这里 monkeypatch 全局库路径指向 tmp 隔离库。
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status) "
                    "VALUES(?,?,0.7,'done')", (pid, uid)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,content,content_hash) "
                    "VALUES(?,'pytest',1,'','h')", (tid,)).lastrowid
    cases = [("test_login_ok", "登录成功并显示欢迎信息",
              "def test_login_ok(): assert login('u','p') 显示欢迎信息"),
             ("test_cart_total", "总计 100", "def test_cart_total(): ..."),
             ("test_bad_import", None, "from cart import discount"),
             ("test_manual_check", "手动验证通过", "def test_manual_check(): ...")]
    for name, exp, code in cases:
        c.execute("INSERT INTO test_cases(script_id,source,name,expected,content) "
                  "VALUES(?,?,?,?,?)", (sid, "ai", name, exp, code))
    c.execute("INSERT INTO test_cases(script_id,source,name,content) VALUES(?, 'imported', "
              "'test_extra', 'x')", (sid,))
    c.execute("""INSERT INTO test_conditions(task_id,requirement_ref,condition,priority)
                 VALUES
                 (?,?,?, '高'),
                 (?,?,?, '中')""", (tid, "登录欢迎", "登录成功后页面应显示欢迎信息",
                                   tid, "导出", "支持导出 PDF 报表"))
    c.execute("""INSERT INTO test_reports(task_id,passed,failed,skipped,coverage_line,defects)
                 VALUES(?,?,?,?,0.85,?)""", (tid, 8, 1, 1,
                                             json.dumps({"failure_kind": "product_defect",
                                                         "stop_reason": "疑似产品缺陷：证人保留"})))
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    for seq, text in enumerate([
            "[evaluate 第1轮] 通过3 失败2 跳过0 覆盖率0.60 指纹abc123 → repair_gen（用例/产品类失败，重新生成用例）",
            "[evaluate 第2轮] 通过8 失败1 跳过1 覆盖率0.85 指纹def456 → stop（疑似产品缺陷）"]):
        c.execute("INSERT INTO agent_messages(session_id,seq,role,content) VALUES(?,?, 'assistant', ?)",
                  (ses, seq, text))
    # 工具调用台账：run_tests 2 ok 1 failed；gen_tests 1 ok
    for name, n_ok, n_fail, ms in (("run_tests", 2, 1, 100), ("gen_tests", 1, 0, 500)):
        skid = c.execute("INSERT INTO skills(name,input_schema) VALUES(?, '{}')", (name,)).lastrowid
        for _ in range(n_ok):
            c.execute("INSERT INTO skill_invocations(task_id,skill_id,args_json,status,duration_ms) "
                      "VALUES(?,?, '{}', 'ok', ?)", (tid, skid, ms))
        for _ in range(n_fail):
            c.execute("INSERT INTO skill_invocations(task_id,skill_id,args_json,status) "
                      "VALUES(?,?, '{}', 'failed')", (tid, skid))
    c.commit()
    yield c
    c.close()


class TestCollectMetrics:
    def test_指标包数字全部来自SQL(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        m = collect_metrics(conn, tid)
        assert m["passed"] == 8 and m["failed"] == 1 and m["skipped"] == 1
        assert m["pass_rate"] == 0.8
        assert m["coverage_line"] == 0.85 and m["coverage_target"] == 0.7
        assert m["failure_kind"] == "product_defect"

    def test_轨迹解析与归因分布(self, conn):
        """老数据兼容路径：任务无 evaluation_rounds 表行（建表前的实验数据），
        轨迹回退解析叙述消息——措辞耦合的旧机制仅为历史数据保留。"""
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        m = collect_metrics(conn, tid)
        assert m["trace_source"] == "legacy_messages"
        assert [t["round"] for t in m["trace"]] == [1, 2]
        assert m["trace"][0]["coverage"] == 0.60 and m["trace"][1]["verdict"] == "stop"
        assert m["verdict_counts"] == {"repair_gen": 1, "stop": 1}

    def test_轨迹优先读结构化表_与消息解耦(self, conn):
        """P1-③：有表行时轨迹只认 evaluation_rounds——叙述消息措辞再变（甚至为垃圾）
        也不影响轨迹，评估节点与指标包从此解耦。"""
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        for r, p, f, s, cov, v in ((1, 3, 2, 0, 0.5, "repair_gen"),
                                   (2, 9, 0, 1, 0.92, "pass")):
            conn.execute(
                """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed,
                                                skipped, coverage_line, verdict)
                   VALUES(?,?,?,?,?,?,?)""", (tid, r, p, f, s, cov, v))
        # 叙述消息全部改成不可解析的垃圾：表优先，垃圾不影响轨迹
        conn.execute("UPDATE agent_messages SET content='evaluate 内部措辞已改版 xx'")
        conn.commit()
        m = collect_metrics(conn, tid)
        assert m["trace_source"] == "evaluation_rounds"
        assert [t["round"] for t in m["trace"]] == [1, 2]
        assert m["trace"][1] == {"round": 2, "passed": 9, "failed": 0,
                                 "skipped": 1, "coverage": 0.92, "verdict": "pass"}
        assert m["verdict_counts"] == {"repair_gen": 1, "pass": 1}

    def test_工具调用成功率与耗时(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        m = collect_metrics(conn, tid)
        assert m["tool_invocations"]["run_tests"]["ok"] == 2
        assert m["tool_invocations"]["run_tests"]["total"] == 3
        assert m["tool_invocations"]["run_tests"]["success_rate"] == round(2 / 3, 4)
        assert m["tool_success_rate"] == 0.75          # 3 ok / 4 total
        assert m["tool_invocations"]["gen_tests"]["duration_ms"] == 500

    def test_六要素完整率与来源分布(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        m = collect_metrics(conn, tid)
        assert m["cases_total"] == 5 and m["cases_by_source"] == {"ai": 4, "imported": 1}
        assert m["six_elem_rate"] == 0.6               # 3/5 有 expected（bad_import/extra 缺）

    def test_无报告行_诚实短路(self, conn):
        uid = conn.execute("SELECT user_id FROM test_tasks LIMIT 1").fetchone()["user_id"]
        pid = conn.execute("SELECT project_id FROM test_tasks LIMIT 1").fetchone()["project_id"]
        tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,?)",
                           (pid, uid)).lastrowid
        conn.commit()
        assert collect_metrics(conn, tid).get("no_report") is True


class TestMapRequirements:
    def test_启发式映射_重叠判覆盖_零重叠判缺口(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        r = map_requirements(conn, tid, api_key=None)   # 无 Key → 确定性通道
        assert r["source"] == "heuristic" and r["conditions_total"] == 2
        by_cond = {m["condition"][:4]: m for m in r["matrix"]}
        assert by_cond["登录成功"]["status"] == "covered"   # 与 test_cases 内容实质重叠
        assert by_cond["支持导出"]["status"] == "gap"       # 零重叠 → 缺口（宁报 gap 不漏报）
        assert r["covered"] == 1 and r["gap"] == 1

    def test_矩阵快照落库且幂等覆盖(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        map_requirements(conn, tid, api_key=None)
        map_requirements(conn, tid, api_key=None)       # 第二次覆盖不叠加
        n = conn.execute("SELECT COUNT(*) AS n FROM rtm_maps WHERE task_id=?",
                         (tid,)).fetchone()["n"]
        assert n == 1


class TestReviewRules:
    def test_数字一致_通过(self, conn):
        m = {"passed": 8, "failed": 1, "skipped": 1, "coverage_line": 0.85,
             "failure_kind": None, "failed_total": None}
        assert review_rules("本轮通过 8、失败 1、跳过 1，覆盖率 85%。", m, "pass") == []

    def test_叙述数字与数据不符_reject(self):
        m = {"passed": 8, "failed": 1, "skipped": 0, "coverage_line": 0.85}
        reasons = review_rules("本轮通过 9、失败 0，覆盖率 85%。", m, "pass")
        assert any("不符" in r for r in reasons)

    def test_覆盖率越界_reject(self):
        m = {"passed": 8, "failed": 0, "skipped": 0, "coverage_line": 1.2}
        assert any("越界" in r for r in review_rules("覆盖率 120%", m, "pass"))

    def test_缺陷写成已解决_reject_防造假红线(self):
        m = {"passed": 8, "failed": 1, "skipped": 0, "coverage_line": 0.85,
             "failure_kind": "product_defect"}
        reasons = review_rules("缺陷已解决，全部修复。通过 8、失败 1。", m, "stop")
        assert any("防造假" in r or "产品缺陷" in r for r in reasons)

    def test_失败却称全部通过_reject(self):
        m = {"passed": 8, "failed": 1, "skipped": 0, "coverage_line": 0.85}
        assert any("全部通过" in r for r in review_rules("用例全部通过。", m, "pass"))

    def test_未通过不误抓通过数(self):
        m = {"passed": 8, "failed": 1, "skipped": 0, "coverage_line": 0.85}
        assert review_rules("另有 1 条未通过 2 项子检查。通过 8、失败 1。", m, "pass") == []


class TestFallbackReport:
    def test_降级报告自洽_必过自家规则闸(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        m = collect_metrics(conn, tid)
        rtm = map_requirements(conn, tid, api_key=None)
        text = fallback_report(m, rtm)
        assert review_rules(text, m, "stop") == []      # 数字逐一相符（确定性模板的契约）
        assert "疑似产品缺陷" in text                     # 缺陷如实呈现

    def test_skill包装层_注册与执行(self, conn):
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        skills = build_eval_skills()
        assert set(skills) == {"collect_metrics", "map_requirements", "gen_report",
                               "review_report", "gen_charts"}
        out = json.loads(skills["collect_metrics"].func(tid, {}))
        assert out["passed"] == 8
        out = json.loads(skills["review_report"].func(tid, {"verdict": "stop"}))
        assert out["status"] in ("pass", "reject")       # 无 Key 时规则闸兜底可执行


class TestSnapshotConsistency:
    """单快照语义（run46 根修）：叙述/审查/图表必须派生自同一份指标包——
    skill 层吃下传快照时不许再自取 collect_metrics（两次 collect 之间评估工具
    自身台账必然增长，这正是 run46 叙述与审查数字漂移的根源）。"""

    _SNAP = {"task_id": 1, "report_id": 1, "passed": 8, "failed": 1, "skipped": 1,
             "coverage_line": 0.85, "coverage_target": 0.7,
             "trace": [{"round": 1, "coverage": 0.6, "verdict": "stop"}],
             "verdict_counts": {"stop": 1},
             "tool_invocations": {"run_tests": {"ok": 2, "total": 3, "duration_ms": 100}}}

    def test_charts_吃下传快照_不得自取(self, conn, monkeypatch, tmp_path):
        import test_agent.skills.evaluate as ev
        monkeypatch.setattr(ev.config, "DATA_DIR", tmp_path)   # 出图落 tmp，不污染 data/
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]

        def _boom(*a, **k):
            raise AssertionError("下传快照时不得再调 collect_metrics（漂移根源）")
        monkeypatch.setattr(ev, "collect_metrics", _boom)
        out = json.loads(build_eval_skills()["gen_charts"].func(
            tid, {"metrics": dict(self._SNAP, task_id=tid, report_id=1),
                  "rtm": {"matrix": []}}))
        assert len(out["charts"]) == 3                  # 趋势+判定+工具耗时，全来自下传快照

    def test_charts_无下传时才自取_机制对称(self, conn, monkeypatch, tmp_path):
        import test_agent.skills.evaluate as ev
        monkeypatch.setattr(ev.config, "DATA_DIR", tmp_path)
        tid = conn.execute("SELECT id FROM test_tasks LIMIT 1").fetchone()["id"]
        calls, real = [], ev.collect_metrics

        def _spy(c, t, *a, **k):
            calls.append(t)
            return real(c, t, *a, **k)
        monkeypatch.setattr(ev, "collect_metrics", _spy)
        build_eval_skills()["gen_charts"].func(tid, {})   # 独立调用：允许自取
        assert calls == [tid]
