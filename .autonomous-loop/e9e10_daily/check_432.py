"""只读补取 task 432（auto 验证格）的 L1 终态摘要，供格 JSON 回填。零写入。"""
import json
import sqlite3

c = sqlite3.connect(r"file:d:/test agent_docker/data/test_agent.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
out = {}
out["task"] = dict(c.execute(
    "SELECT id, status FROM test_tasks WHERE id=432").fetchone())
rep = c.execute("SELECT * FROM test_reports WHERE task_id=432 ORDER BY id DESC LIMIT 1").fetchone()
if rep:
    out["report"] = {k: rep[k] for k in rep.keys()
                     if k in ("verdict", "passed", "failed", "coverage_line", "round_no",
                              "stop_reason", "total", "skipped")}
r432 = [dict(r) for r in c.execute(
    "SELECT decision, COUNT(*) n FROM review_decisions WHERE task_id=432 "
    "GROUP BY decision ORDER BY n DESC")]
out["decisions"] = r432
out["rounds"] = [dict(r) for r in c.execute(
    "SELECT round_no, passed, failed FROM evaluation_rounds WHERE task_id=432 "
    "ORDER BY round_no")]
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
c.close()
