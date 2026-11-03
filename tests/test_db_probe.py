"""DB 感知测试环境 v1：家族检测（确定性）/ sqlite 自动建库链路 / 服务端库诚实降级。tmp 工作区。"""
import json
import sqlite3

from test_agent.skills.db_probe import detect_db_usage, prepare_database


def _ws(tmp, files):
    ws = tmp / "proj"
    ws.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        f = ws / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return ws


def test_detect_families(tmp_path):
    ws = _ws(tmp_path, {"lib/db.js": "const mysql = require('mysql2/promise');\nmodule.exports = mysql;"})
    assert detect_db_usage(ws)["family"] == "mysql"
    ws2 = _ws(tmp_path / "a", {"server.js": "const db = require('better-sqlite3')('app.db');"})
    assert detect_db_usage(ws2)["family"] == "sqlite"
    ws3 = _ws(tmp_path / "b", {"app.py": "print('no db here')"})
    assert detect_db_usage(ws3)["family"] is None


def test_detect_scripts_and_sql(tmp_path):
    ws = _ws(tmp_path, {"package.json": json.dumps({"scripts": {"migrate": "node mig.js", "start": "node s.js"}}),
                        "migrations/001_init.sql": "CREATE TABLE t(a);",
                        "lib/x.js": "require('better-sqlite3')"})
    det = detect_db_usage(ws)
    assert det["family"] == "sqlite" and "migrate" in det["scripts"]
    assert any("001_init.sql" in f for f in det["sql_files"])


def test_detect_skips_junk_dirs(tmp_path):
    # 排除目录里的 DB 特征不计入（node_modules 里的 mysql2 不代表项目用 mysql）
    ws = _ws(tmp_path, {"node_modules/x/index.js": "require('mysql2/promise')",
                        "app.js": "console.log(1)"})
    assert detect_db_usage(ws)["family"] is None


def test_prepare_sqlite_python(tmp_path):
    ws = _ws(tmp_path, {"schema.sql": "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT);",
                        "app.py": "import sqlite3"})
    det = {"family": "sqlite", "sql_files": ["schema.sql"], "scripts": []}

    class FakeRunner:  # python 路径走宿主 sqlite3，不用容器
        pass
    out = prepare_database(FakeRunner(), None, det, ws, "python")
    assert out["status"] == "ok", out
    conn = sqlite3.connect(ws / "test_app.db")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='items'").fetchone()
    conn.close()


def test_prepare_mysql_honest_skip(tmp_path):
    ws = _ws(tmp_path / "m", {"lib/db.js": "require('mysql2/promise')"})
    det = detect_db_usage(ws)
    out = prepare_database(None, None, det, ws, "javascript")
    assert out["status"] == "skip" and "mysql" in out["note"]
