"""Test Agent CLI —— 端到端入口（L1 界面完成前的操作面）。

用法:
  python cli.py new <repo_url或本地路径> "<自然语言需求>"   # 新建测试任务（未确认）
  python cli.py confirm <task_id>                          # 人工确认（放行执行）
  python cli.py run <task_id>                              # 下发执行（R-Loop 全链路）
  python cli.py status <task_id>                           # 查看任务状态
  python cli.py report <task_id>                           # 查看测试报告
  python cli.py log <task_id>                              # 查看 Agent 会话轨迹
"""
import json
import sys

from test_agent.graph.builder import run_task
from test_agent.store import db

USAGE = __doc__


def _default_uid(conn) -> int:
    """L1 上线前的简化：单开发者账号 dev。"""
    conn.execute("INSERT OR IGNORE INTO users(username,password_hash,role) VALUES('dev','x','developer')")
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username='dev'").fetchone()["id"]


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE)
        return 1
    cmd = sys.argv[1]
    db.init_db()
    conn = db.get_conn()

    # —— 新建任务 ——
    if cmd == "new" and len(sys.argv) == 4:
        repo_url, req = sys.argv[2], sys.argv[3]
        uid = _default_uid(conn)
        name = repo_url.rstrip("/").split("/")[-1] or "project"
        pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                           (uid, repo_url, name)).lastrowid
        tid = conn.execute(
            "INSERT INTO test_tasks(project_id,user_id,requirement_text) VALUES(?,?,?)",
            (pid, uid, req)).lastrowid
        conn.commit()
        print(f"任务已创建: id={tid}（confirmed=0，先 confirm 再 run —— 人工确认门）")
        return 0

    # —— 单任务操作 ——
    if cmd in ("confirm", "run", "status", "report", "log") and len(sys.argv) == 3:
        tid = int(sys.argv[2])
        task = conn.execute("SELECT * FROM test_tasks WHERE id=?", (tid,)).fetchone()
        if not task:
            print("任务不存在")
            return 1

        if cmd == "confirm":
            conn.execute(
                "UPDATE test_tasks SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
                (_default_uid(conn), tid))
            conn.commit()
            print(f"任务 {tid} 已确认（confirmed=1），可执行 run 下发")
            return 0

        if cmd == "run":
            final = run_task(tid)
            print(json.dumps({k: final.get(k) for k in
                              ("verdict", "stop_reason", "passed", "failed",
                               "coverage_line", "round_no")},
                             ensure_ascii=False, indent=2))
            return 0

        if cmd == "status":
            print(json.dumps(dict(task), ensure_ascii=False, indent=2))
            return 0

        if cmd == "report":
            rows = conn.execute("SELECT * FROM test_reports WHERE task_id=?", (tid,)).fetchall()
            if not rows:
                print("暂无报告")
                return 1
            for r in rows:
                print(json.dumps(dict(r), ensure_ascii=False, indent=2))
            return 0

        if cmd == "log":
            for r in conn.execute("""SELECT m.seq, m.content FROM agent_messages m
                                     JOIN agent_sessions s ON s.id = m.session_id
                                     WHERE s.task_id = ? ORDER BY m.seq""", (tid,)):
                print(f"[{r['seq']:02d}] {r['content'][:300]}")
            return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
