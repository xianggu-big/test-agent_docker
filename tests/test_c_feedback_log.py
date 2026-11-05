"""C（任务卡-369 缺陷修复-20261105）：反馈原文留档——expected_basis=「用户反馈」的
用例此前无法溯源（原文只在内存传递）。留痕到 agent_messages（role=user），
「用户反馈」basis 可软链回查到消息 id；session 不可得/留痕失败不阻断主链路。"""
import pytest

from test_agent import config
from test_agent.graph.builder import log_feedback_message
from test_agent.store import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'ready_for_review',1)", (pid, uid)).lastrowid
    yield c, tid
    c.close()


def test_feedback_logged_and_traceable(env):
    c, tid = env
    c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,))
    c.commit()
    mid = log_feedback_message(c, tid, 1, "给删除功能补一条空 id 的用例")
    assert mid is not None
    row = c.execute("SELECT role, content FROM agent_messages WHERE id=?", (mid,)).fetchone()
    assert row["role"] == "user" and row["content"].startswith("[feedback 第1轮]")
    assert "空 id" in row["content"]                 # 原文完整可回查


def test_feedback_without_session_returns_none(env):
    c, tid = env
    assert log_feedback_message(c, tid, 2, "第二条反馈") is None   # 不炸主链路
