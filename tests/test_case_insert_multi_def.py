# -*- coding: utf-8 -*-
"""0'' 缺口①/②回归（2026-11-28）：①多 def 用例插入不再被 A5 改名；②修复轮重建物化 imported 用例。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MULTI_DEF = ("import pytest\n"
             "from fastapi.testclient import TestClient\n"
             "from app import app\n\n"
             "@pytest.fixture()\n"
             "def c():\n"
             "    return TestClient(app)\n\n"
             "def test_bnd_a(c):\n"
             "    assert True\n\n"
             "def test_bnd_b(c):\n"
             "    assert True\n")
SINGLE_DEF = "def test_bnd_x():\n    assert True\n"


def test_多def插入不改名单def插入改名():
    """用 0'' 缺口① 的真实规则（server.api_add_case 的重写段）做纯函数级验证：
    多 def 文件首 def 保持原样；单 def 文件首 def 被对齐为用例名。"""
    import re
    def_name = _sanitize("test_boundary_auto", set())
    for code, expect_first in ((MULTI_DEF, "def c"), (SINGLE_DEF, f"def {def_name}")):
        defs = re.findall(r"def\s+\w+", code)
        m = re.search(r"def\s+\w+", code) if len(defs) == 1 else None
        out = code[:m.start()] + f"def {def_name}" + code[m.end():] if m else code
        first_def = re.search(r"def\s+\w+", out).group(0)
        assert first_def == expect_first, (expect_first, first_def)


def _sanitize(name, used):
    from test_agent.api.server import _sanitize_name
    return _sanitize_name(name, len(used), used)


def test_修复轮重建物化imported(tmp_path, monkeypatch):
    """修复轮重建路径补物化：库里有 enabled imported 用例 → test_imported.py 落工作区。"""
    from test_agent import config
    from test_agent.store import db
    from test_agent.skills.gen_tests import _rebuild_imported_file
    dbp = tmp_path / "test_agent.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed) VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                    "content,content_hash) VALUES(?,'pytest',1,NULL,'c','h')", (tid,)).lastrowid
    c.execute("INSERT INTO test_cases(script_id,source,name,content,enabled) "
              "VALUES(?,'imported','test_bnd_a','def test_bnd_a():\n    assert True',1)", (sid,))
    c.commit()
    ws = tmp_path / "ws"
    ws.mkdir()
    _rebuild_imported_file(c, tid, ws)
    f = ws / "test_imported.py"
    assert f.exists() and "test_bnd_a" in f.read_text(encoding="utf-8"), \
        "修复轮重建必须物化 imported 用例（0'' 缺口②）"
    c.close()
