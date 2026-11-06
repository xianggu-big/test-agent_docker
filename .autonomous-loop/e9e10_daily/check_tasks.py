"""只读核查：小样任务(420+)的执行状态/工作区/代审决策/轮次。零写入。"""
import json
import sqlite3

DB = r"d:\test agent_docker\data\test_agent.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
out = {}
out["recent_tasks"] = [dict(r) for r in c.execute(
    "SELECT id, status, confirmed, ai_reviewer, created_at FROM test_tasks "
    "WHERE id >= 418 ORDER BY id")]
out["llm_calls"] = [dict(r) for r in c.execute(
    "SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) total FROM llm_calls")]
out["recent_llm"] = [dict(r) for r in c.execute(
    "SELECT task_id, COUNT(*) n, SUM(total_tokens) t FROM llm_calls WHERE task_id>=418 "
    "GROUP BY task_id ORDER BY task_id")]
out["review_decisions"] = [dict(r) for r in c.execute(
    "SELECT task_id, decision, target FROM review_decisions WHERE task_id>=418")]
out["rounds"] = [dict(r) for r in c.execute(
    "SELECT * FROM evaluation_rounds WHERE task_id>=418 ORDER BY task_id, round_no LIMIT 6")]
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
c.close()
