"""只读盘点：E9 已落库用例总数/任务数（口径：E9 波次产生的任务，task_id>=418）。零写入。"""
import json
import sqlite3

c = sqlite3.connect(r"file:d:/test agent_docker/data/test_agent.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
out = {}
out["tasks_e9"] = c.execute(
    "SELECT COUNT(*) n FROM test_tasks WHERE id>=418").fetchone()["n"]
out["cases_total"] = c.execute(
    """SELECT COUNT(*) n FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
       WHERE s.task_id>=418""").fetchone()["n"]
out["cases_by_arm"] = [dict(r) for r in c.execute(
    """SELECT CASE WHEN t.id IN (430,432) THEN 'A2_verify'
                  ELSE substr(p.name, 1, 4) END arm,
              COUNT(c.id) cases, COUNT(DISTINCT t.id) tasks
       FROM test_tasks t JOIN projects p ON p.id=t.project_id
       LEFT JOIN test_scripts s ON s.task_id=t.id
       LEFT JOIN test_cases c ON c.script_id=s.id
       WHERE t.id>=418 GROUP BY arm ORDER BY arm""")]
out["calls_e9"] = c.execute(
    "SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) t FROM llm_calls WHERE task_id>=418"
).fetchone()
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
c.close()
