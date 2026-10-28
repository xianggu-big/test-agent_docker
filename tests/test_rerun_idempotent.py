"""重跑幂等回归：任务重跑再次进图时，落库与工作区产物不得翻倍/丢失。

- P1-①：plan 节点 test_conditions 裸 INSERT 会随重跑翻倍 → delete-first 以本轮为准
- P1-②：clone rmtree 工作区后 test_imported.py 无人重建（DB 指标仍统计）→ 从库重建
"""
import langchain_openai
import pytest

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.store import db


class _FakeLLM:
    """plan 节点的可控假 LLM：类属性 payload 控制本次 invoke 的返回。"""
    payload = "{}"

    def __init__(self, **kw):
        pass

    def invoke(self, msgs, **kw):
        return type("R", (), {"content": _FakeLLM.payload})()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PLAN_ENABLED", True)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeLLM)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    deps = GraphDeps(conn=c, runner=type("R", (), {})(), registry=None, skills={})
    nodes = make_nodes(deps)
    yield c, nodes, tid, ses
    c.close()


def test_plan_rerun_does_not_double_conditions(env):
    """P1-①：同一任务两次进 plan（重跑），条件以最新一轮为准，行数不翻倍。"""
    c, nodes, tid, ses = env
    state = {"task_id": tid, "session_id": ses, "requirement": "支持加法与减法"}

    _FakeLLM.payload = ('{"conditions": ['
                        '{"condition": "两数相加返回和", "priority": "高"},'
                        '{"condition": "减法返回差", "priority": "中"}]}')
    nodes["plan"](state)
    assert c.execute("SELECT COUNT(*) FROM test_conditions WHERE task_id=?",
                     (tid,)).fetchone()[0] == 2

    # 重跑：LLM 这轮拆出 3 条（内容不同），旧 2 条应被整体替换而非追加成 5
    _FakeLLM.payload = ('{"conditions": ['
                        '{"condition": "加法交换律", "priority": "高"},'
                        '{"condition": "零元加法", "priority": "中"},'
                        '{"condition": "负数加法", "priority": "低"}]}')
    nodes["plan"](state)
    rows = c.execute("SELECT condition FROM test_conditions WHERE task_id=? ORDER BY id",
                     (tid,)).fetchall()
    assert [r["condition"] for r in rows] == ["加法交换律", "零元加法", "负数加法"]


def _insert_imported_case(c, tid, name, content):
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,content,content_hash) "
                    "VALUES(?,'pytest',1,'','h')", (tid,)).lastrowid
    return c.execute(
        "INSERT INTO test_cases(script_id,source,name,content,enabled) "
        "VALUES(?,'imported',?,?,1)", (sid, name, content)).lastrowid


def test_clone_rerun_rebuilds_imported_file(env, tmp_path):
    """P1-②：clone rmtree 工作区后，imported 用例从库重建，重跑不丢执行语义。"""
    c, nodes, tid, ses = env
    _insert_imported_case(c, tid, "test_manual_a",
                          "def test_manual_a():\n    assert 1 + 1 == 2\n")

    src = tmp_path / "repo_src"
    src.mkdir()
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    ws.mkdir(parents=True)
    (ws / "test_imported.py").write_text("# 上一轮遗留\n", encoding="utf-8")  # 将被 rmtree

    nodes["clone"]({"task_id": tid, "session_id": ses, "repo_url": str(src)})

    rebuilt = ws / "test_imported.py"
    assert rebuilt.exists(), "重跑后 imported 用例文件应从库重建"
    body = rebuilt.read_text(encoding="utf-8")
    assert "test_manual_a" in body and "assert 1 + 1 == 2" in body
    assert (ws / "calc.py").exists()  # 快照照常


def test_clone_rerun_no_imported_writes_nothing(env, tmp_path):
    """无 imported 用例的任务重跑：不生成空 test_imported.py（不污染容器快照）。"""
    c, nodes, tid, ses = env
    src = tmp_path / "repo_src2"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")

    nodes["clone"]({"task_id": tid, "session_id": ses, "repo_url": str(src)})

    ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
    assert not (ws / "test_imported.py").exists()
