"""review 节点图级单测：报告二次审查的回炉语义（双闸 + 预算 + stop 不可推翻）。"""
import json

import pytest

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.skills.builtin import build_skills
from test_agent.skills.evaluate import build_eval_skills
from test_agent.skills.registry import Skill, SkillRegistry
from test_agent.store import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)   # gen_charts 出图落 tmp，不污染 data/
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")  # skill func 线程池内自建连接走全局路径
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'done',1)", (pid, uid)).lastrowid
    sid_script = c.execute("INSERT INTO test_scripts(task_id,framework,version,content,content_hash) "
                           "VALUES(?,'pytest',1,'','h')", (tid,)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    runner = type("R", (), {})()                        # review 节点不用 runner
    registry = SkillRegistry(c)
    skills = {**build_skills(runner), **build_eval_skills()}
    for s in skills.values():
        registry.register(s)
    deps = GraphDeps(conn=c, runner=runner, registry=registry, skills=skills)
    # gen_report 换 stub：审查语义测试自己预置 narrative（gen_report 本体已有降级自洽单测），
    # 否则无 Key 降级模板会把预设叙述覆盖成自洽文本，reject 路径永远打不出来
    skills["gen_report"] = Skill(name="gen_report", description="stub",
                                 func=lambda tid, args: "{}", risk_level="low")
    nodes = make_nodes(deps)
    yield c, nodes["review"], tid, ses, sid_script
    c.close()


def _add_report(c, tid, sid_script, passed, failed, cov, kind=None):
    return c.execute(
        """INSERT INTO test_reports(task_id,module_id,passed,failed,skipped,coverage_line,defects)
           VALUES(?,?,?,?,?,?,?)""",
        (tid, None, passed, failed, 0, cov,
         json.dumps({"failure_kind": kind, "stop_reason": "达标"}))).lastrowid


def _state(tid, ses, verdict, round_no=1):
    return {"task_id": tid, "session_id": ses, "verdict": verdict, "round_no": round_no}


def _set_narrative(c, report_id, text):
    c.execute("UPDATE test_reports SET narrative=? WHERE id=?", (text, report_id))
    c.commit()


def test_pass_叙述自洽_审查通过不回炉(env):
    c, review, tid, ses, sid = env
    rid = _add_report(c, tid, sid, 10, 0, 0.9)
    _set_narrative(c, rid, "本轮通过 10、失败 0、跳过 0，覆盖率 90%。全部达标。")
    out = review(_state(tid, ses, "pass"))
    assert out == {"review_status": "pass"}             # 审查通过
    rev = json.loads(c.execute("SELECT review FROM test_reports WHERE id=?",
                               (rid,)).fetchone()["review"])
    assert rev["status"] == "pass"
    assert c.execute("SELECT status FROM test_tasks WHERE id=?",
                     (tid,)).fetchone()["status"] == "done"   # 终态恢复（review 入口的 reporting 是过程态）


def test_pass_叙述数字失真_审查reject标记不回炉(env):
    c, review, tid, ses, sid = env
    rid = _add_report(c, tid, sid, 10, 0, 0.9)
    _set_narrative(c, rid, "本轮通过 9、失败 0，覆盖率 90%。")   # 通过数与数据不符
    out = review(_state(tid, ses, "pass", round_no=1))
    assert out == {"review_status": "rejected"}   # v1 收口：不回炉，如实标记


def test_pass_审查reject_如实标记放行(env):
    c, review, tid, ses, sid = env
    rid = _add_report(c, tid, sid, 10, 0, 0.9)
    _set_narrative(c, rid, "本轮通过 9，覆盖率 90%。")
    out = review(_state(tid, ses, "pass", round_no=config.MAX_REPAIR_ROUNDS))
    assert out == {"review_status": "rejected"}   # v1 收口：不回炉，如实标记


def test_stop_产品缺陷_审查不可推翻(env):
    """防造假红线：product_defect 的 stop 判定即使叙述失真也不回炉（证人冻结语义）。"""
    c, review, tid, ses, sid = env
    rid = _add_report(c, tid, sid, 8, 1, 0.85, kind="product_defect")
    _set_narrative(c, rid, "缺陷已解决，全部修复。通过 8、失败 1。")
    out = review(_state(tid, ses, "stop"))
    assert out == {"review_status": "rejected"}   # 谎报"缺陷已解决"→ 如实标记（不改判判定）
    rev = json.loads(c.execute("SELECT review FROM test_reports WHERE id=?",
                               (rid,)).fetchone()["review"])
    assert rev["status"] == "reject"                    # 违规留痕，但判定不改


def test_无报告行_诚实跳过(env):
    c, review, tid, ses, sid = env                      # 不造 report 行（static_only 短路链路）
    assert review(_state(tid, ses, "stop")) == {}
    assert c.execute("SELECT status FROM test_tasks WHERE id=?",
                     (tid,)).fetchone()["status"] == "failed"  # 终态跟随执行判定（stop→failed）


def test_评估链单快照_collect恰好一次(env):
    """run46 漂移根修的机制锁：叙述/审查/图表全链只允许 collect 一次——
    gen_charts 若自取（旧实现），账本里 collect_metrics 会变成 2 次，漂移即回归。"""
    c, review, tid, ses, sid = env
    rid = _add_report(c, tid, sid, 10, 0, 0.9)
    _set_narrative(c, rid, "本轮通过 10、失败 0、跳过 0，覆盖率 90%。")
    out = review(_state(tid, ses, "pass"))
    assert out == {"review_status": "pass"}
    rows = c.execute("""SELECT sk.name, COUNT(*) AS n FROM skill_invocations i
                        JOIN skills sk ON sk.id = i.skill_id
                        WHERE i.task_id=? GROUP BY sk.name""", (tid,)).fetchall()
    counts = {r["name"]: r["n"] for r in rows}
    assert counts.get("collect_metrics") == 1        # 单快照：评估链只 collect 一次
    for name in ("map_requirements", "gen_report", "review_report", "gen_charts"):
        assert counts.get(name) == 1, name           # 五工具各恰好一次
