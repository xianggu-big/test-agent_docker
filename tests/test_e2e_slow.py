"""端到端慢速测试：需 Docker + DEEPSEEK_API_KEY，默认跳过。

启用：PowerShell 里 `$env:MTA_E2E="1"` 后再跑 pytest。
验证的是"活的全链路"（真实容器 + 真实 LLM），不追求确定性断言，
只验证终态合法（达标 或 诚实停止）且数据落库完整。
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MTA_E2E") != "1",
                                reason="需 Docker 与 DeepSeek Key；设 MTA_E2E=1 启用")


def test_full_pipeline_reaches_legal_terminal_state(tmp_path):
    from test_agent.graph.builder import run_task
    from test_agent.store import db

    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT OR IGNORE INTO users(username,password_hash,role) VALUES('e2e','x','developer')")
    uid = conn.execute("SELECT id FROM users WHERE username='e2e'").fetchone()["id"]
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?, 'local://e2e', 'e2e冒烟')",
                       (uid,)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by) "
        "VALUES(?,?,?,?,1)", (pid, uid, "测试加法", 1)).lastrowid
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    conn.execute("UPDATE projects SET repo_url=? WHERE id=?", (str(proj), pid))
    conn.commit()

    final = run_task(tid)
    assert final["verdict"] in ("pass", "stop")           # 终态合法
    assert bool(final.get("stop_reason"))                  # 一定带原因（诚实纪律）
    report = conn.execute("SELECT COUNT(*) AS n FROM test_reports WHERE task_id=?",
                          (tid,)).fetchone()["n"]
    assert report == 1                                     # 报告落库
