"""Test Agent CLI —— 端到端入口（L1 界面完成前的操作面）。

用法:
  python cli.py new <repo_url或本地路径> "<自然语言需求>"   # 新建测试任务（未确认）
  python cli.py confirm <task_id>                          # 人工确认（放行执行）
  python cli.py run <task_id>                              # 下发执行（R-Loop 全链路）
  python cli.py status <task_id>                           # 查看任务状态
  python cli.py report <task_id>                           # 查看测试报告（标注最新）
  python cli.py log <task_id>                              # 查看 Agent 会话轨迹（按会话分组）
  python cli.py clean                                      # 清理任务工作区快照（data/workspaces）
"""
import json
import logging
import shutil
import sys

from test_agent import config
from test_agent.graph.builder import run_task
from test_agent.store import db

USAGE = __doc__


def _default_uid(conn) -> int:
    """L1 上线前的简化：单开发者账号 dev。"""
    conn.execute("INSERT OR IGNORE INTO users(username,password_hash,role) VALUES('dev','x','developer')")
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username='dev'").fetchone()["id"]


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
            try:
                final = run_task(tid)
            except Exception as e:
                # Docker daemon 未启动是最常见的环境坑（pitfalls #2），友好提示不甩栈
                if "docker" in type(e).__module__.lower() or "npipe" in str(e) \
                        or "docker API" in str(e):
                    print("Docker daemon 未连接：请先启动 Docker Desktop（约 1 分钟就绪）后重试")
                    return 1
                raise
            print(json.dumps({k: final.get(k) for k in
                              ("verdict", "stop_reason", "passed", "failed",
                               "coverage_line", "round_no")},
                             ensure_ascii=False, indent=2))
            return 0

        if cmd == "status":
            print(json.dumps(dict(task), ensure_ascii=False, indent=2))
            return 0

        if cmd == "report":
            rows = conn.execute("SELECT * FROM test_reports WHERE task_id=? ORDER BY id",
                                (tid,)).fetchall()
            if not rows:
                print("暂无报告")
                return 1
            latest_id = rows[-1]["id"]  # 多次 run 会有多份历史报告，标注最新一份
            for r in rows:
                tag = "（最新）" if r["id"] == latest_id else ""
                print(f"—— 报告 #{r['id']}{tag} ——")
                print(json.dumps(dict(r), ensure_ascii=False, indent=2))
            return 0

        if cmd == "log":
            # 按会话分组显示：同一任务多次 run 各自成段，序号不交错
            sessions = conn.execute(
                "SELECT id, started_at FROM agent_sessions WHERE task_id=? ORDER BY id",
                (tid,)).fetchall()
            if not sessions:
                print("暂无会话记录")
                return 1
            for s in sessions:
                print(f"—— 会话 #{s['id']}（开始于 {s['started_at']}）——")
                for r in conn.execute("""SELECT seq, content FROM agent_messages
                                         WHERE session_id = ? ORDER BY seq""", (s["id"],)):
                    print(f"[{r['seq']:02d}] {r['content'][:300]}")
            return 0

    # —— 工作区清理 ——
    if cmd == "clean":
        ws_root = config.DATA_DIR / "workspaces"
        if not ws_root.exists():
            print("没有可清理的工作区")
            return 0
        n = 0
        for d in ws_root.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
                n += 1
        print(f"已清理 {n} 个任务工作区（{ws_root}）")
        return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
