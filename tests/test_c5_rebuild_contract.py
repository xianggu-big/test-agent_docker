# -*- coding: utf-8 -*-
"""C5（2026-11-27 总包）契约测试：放行后工作区=审查结论。

两阶段"布考场"按 test_cases.enabled 重写工作区生成文件（rebuild_gen_files_from_db）。
本测试锁契约：评审页停用（DB enabled=0）后，布考场文件不再含被停用用例；
全停用时文件零用例（零可用用例 → 阶段二会重新生成一轮——2644 实锤的已知窗口，
行为如实保留并在两阶段设计文档写明，不在此测试范围）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CONTENT = (
    "import json\n"
    "\n"
    "def test_a_enabled():\n"
    "    assert 1 == 1\n"
    "\n"
    "def test_b_disabled():\n"
    "    assert 2 == 2\n"
)


def _env(tmp_path):
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "test_agent.db"
    c = db.get_conn(dbp)
    db.init_db(dbp)
    monkey = None
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed) VALUES(?,?,0.7,'ready_for_review',1)", (pid, uid)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                    "content,content_hash) VALUES(?,'pytest',1,'test_gen_r1.py',?, 'h')",
                    (tid, CONTENT)).lastrowid
    for name in ("test_a_enabled", "test_b_disabled"):
        c.execute("INSERT INTO test_cases(script_id,source,name,content,enabled) "
                  "VALUES(?,'ai',?,'c',1)", (sid, name))
    c.commit()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_gen_r1.py").write_text(CONTENT, encoding="utf-8")
    return config, c, tid, ws


def test_rebuild_excludes_disabled_case(tmp_path):
    """停用 test_b_disabled 后布考场重写：文件不再含 b，仍含 a（放行后工作区=审查结论）。"""
    from test_agent.skills.gen_tests import rebuild_gen_files_from_db
    _config, c, tid, ws = _env(tmp_path)
    c.execute("UPDATE test_cases SET enabled=0 WHERE name='test_b_disabled'")
    c.commit()
    n = rebuild_gen_files_from_db(c, tid, ws)
    assert n == 1
    text = (ws / "test_gen_r1.py").read_text(encoding="utf-8")
    assert "test_b_disabled" not in text, "被停用用例必须从布考场摘除（此前只在库生效）"
    assert "def test_a_enabled" in text, "保留用例必须仍在场"


def test_rebuild_all_disabled_leaves_zero_cases(tmp_path):
    """全停用：布考场零用例——阶段二将重新生成一轮（2644 实锤窗口，行为如实保留）。"""
    from test_agent.skills.gen_tests import rebuild_gen_files_from_db
    _config, c, tid, ws = _env(tmp_path)
    c.execute("UPDATE test_cases SET enabled=0")
    c.commit()
    rebuild_gen_files_from_db(c, tid, ws)
    text = (ws / "test_gen_r1.py").read_text(encoding="utf-8")
    assert "def test_" not in text, "全停用后布考场不得残留任何用例函数（imports 可留）"
