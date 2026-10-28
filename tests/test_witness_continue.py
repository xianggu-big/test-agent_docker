"""V8 证人冻结+继续补测：evaluate 节点图级行为（X 格循环类开关的表达前提）。

锁定三件事：
- 开关开：断言失败用例累积进 state.witness_nodeids，其失败行不进指纹（不误触熔断），
  product_defect 不再首轮 stop——按覆盖率继续补测，非证人全过+达标才以证人上报终态
- 开关关：与既有口径逐字节一致（witness 恒空、指纹不排除、product_defect 首轮 stop）
- 轨迹落库（evaluation_rounds）不受开关影响，每轮照常结构化记录
"""
import langchain_openai

from test_agent import config
from test_agent.graph.nodes import GraphDeps, make_nodes
from test_agent.store import db


class _FakeLLM:
    payload = "{}"

    def __init__(self, **kw):
        pass

    def invoke(self, msgs, **kw):
        return type("R", (), {"content": _FakeLLM.payload})()


def _stdout(failed: str, coverage: str) -> str:
    """构造带覆盖率 TOTAL 行的多失败输出（与真实 pytest -rfE 格式同源）。"""
    return (f".F\n{failed}\n\n"
            f"Name    Stmts   Miss  Cover\n"
            f"------------------------------\n"
            f"cart.py    10      2   {coverage}\n"
            f"------------------------------\n"
            f"TOTAL      10      2   {coverage}\n")


WITNESS_FAIL = "FAILED test_cart.py::test_witness - AssertionError: assert 2.0 == 5\n"
OTHER_FAIL = "FAILED test_cart.py::test_other - TypeError: boom\n"


def _make_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
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
    return c, nodes, tid, ses


def test_v8_on_witness_accumulates_and_fingerprint_excludes_it(tmp_path, monkeypatch):
    """开关开：证人当轮产生当轮生效——state 累积证人，指纹只含非证人失败。"""
    monkeypatch.setattr(config, "WITNESS_CONTINUE_ENABLED", True)
    c, nodes, tid, ses = _make_env(tmp_path, monkeypatch)
    state = {"task_id": tid, "session_id": ses, "round_no": 1,
             "last_exit": 1, "last_stdout": _stdout(WITNESS_FAIL + OTHER_FAIL, "50%"),
             "last_stderr": ""}
    out = nodes["evaluate"](state)
    assert out["witness_nodeids"] == ["test_cart.py::test_witness"]
    assert out["verdict"] == "repair_gen"          # 证人+非证人失败 → 常规分流
    # 指纹只反映非证人失败（排除后等价于仅 OTHER_FAIL 的指纹）
    from test_agent.graph import repair
    assert out["fingerprint"] == repair.fingerprint_of(OTHER_FAIL, "")
    c.close()


def test_v8_on_only_witness_left_continues_then_stops_on_target(tmp_path, monkeypatch):
    """开关开：后续轮只剩证人失败——覆盖低继续补测、达标后以证人上报终态 stop。"""
    monkeypatch.setattr(config, "WITNESS_CONTINUE_ENABLED", True)
    c, nodes, tid, ses = _make_env(tmp_path, monkeypatch)
    base = {"task_id": tid, "session_id": ses,
            "witness_nodeids": ["test_cart.py::test_witness"],
            "last_exit": 1, "last_stderr": ""}

    out2 = nodes["evaluate"]({**base, "round_no": 2,
                              "last_stdout": _stdout(WITNESS_FAIL, "50%")})
    assert out2["verdict"] == "repair_gen" and out2["fingerprint"] == ""
    assert "继续补测" in out2["stop_reason"]

    out3 = nodes["evaluate"]({**base, "round_no": 3,
                              "last_stdout": _stdout(WITNESS_FAIL, "100%")})
    assert out3["verdict"] == "stop" and out3["failure_kind"] == "product_defect"
    assert "如实上报" in out3["stop_reason"] and "证人" in out3["stop_reason"]
    c.close()


def test_v8_off_behavior_unchanged(tmp_path, monkeypatch):
    """开关关（默认）：product_defect 首轮 stop，witness 恒空、指纹含证人失败。"""
    monkeypatch.setattr(config, "WITNESS_CONTINUE_ENABLED", False)
    c, nodes, tid, ses = _make_env(tmp_path, monkeypatch)
    state = {"task_id": tid, "session_id": ses, "round_no": 1,
             "last_exit": 1, "last_stdout": _stdout(WITNESS_FAIL, "100%"),
             "last_stderr": ""}
    out = nodes["evaluate"](state)
    assert out["verdict"] == "stop" and out["failure_kind"] == "product_defect"
    assert out["witness_nodeids"] == []
    assert out["fingerprint"]                      # 指纹未排除证人
    c.close()


def test_rounds_table_records_v8_trajectory(tmp_path, monkeypatch):
    """轨迹落库不受开关影响：三轮判定全部结构化记录在 evaluation_rounds。"""
    monkeypatch.setattr(config, "WITNESS_CONTINUE_ENABLED", True)
    c, nodes, tid, ses = _make_env(tmp_path, monkeypatch)
    base = {"task_id": tid, "session_id": ses, "last_exit": 1, "last_stderr": ""}
    nodes["evaluate"]({**base, "round_no": 1,
                       "last_stdout": _stdout(WITNESS_FAIL, "50%")})
    nodes["evaluate"]({**base, "round_no": 2,
                       "last_stdout": _stdout(WITNESS_FAIL, "60%")})
    rows = c.execute(
        "SELECT round_no, verdict FROM evaluation_rounds WHERE task_id=? ORDER BY round_no",
        (tid,)).fetchall()
    assert [(r["round_no"], r["verdict"]) for r in rows] == [(1, "repair_gen"),
                                                             (2, "repair_gen")]
    c.close()
