"""数据层回归：建库幂等、表数量、确认门默认值（fail-closed 的根基）。

表数量当前为 27：15 张业务表 + rtm_maps（map_requirements 产物）+
evaluation_rounds（P1-③：R-Loop 每轮评估结构化落库，轨迹唯一真源）+
complexity_hotspots（W4：radon 复杂度热点快照，报告"热点未覆盖"指标数据源）+
llm_calls（P1-E：每次真实 LLM 调用 token 用量，成本-质量曲线数据源）+
test_levels / interface_endpoints / call_edges（四级双轨 P1：级别台账/端点清单/调用边）+
app_logs（黑盒能力补齐阶段1：应用运行时日志分档，log_analyze 证据真源）。
"""
from test_agent import config
from test_agent.store import db


def test_init_db_creates_all_tables(tmp_path):
    assert db.init_db(tmp_path / "t.db") == 31


def test_init_db_idempotent(tmp_path):
    p = tmp_path / "t.db"
    db.init_db(p)
    assert db.init_db(p) == 31  # 第二次不炸也不重建


def test_rtm_conditions_table_exists(tmp_path):
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    n = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master "
                     "WHERE name='test_conditions'").fetchone()["n"]
    assert n == 1


def test_task_defaults_to_unconfirmed(tmp_path):
    """确认门根基：新任务 confirmed 必须默认 0，不确认就绝不能下发。"""
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,?)",
                       (pid, uid)).lastrowid
    conn.commit()
    assert conn.execute("SELECT confirmed FROM test_tasks WHERE id=?", (tid,)).fetchone()["confirmed"] == 0


def test_default_config_target_is_frozen_value():
    """准出标准防线：全局默认阈值应为 0.70，实验前冻结后不得改动。"""
    assert config.COVERAGE_TARGET == 0.70
