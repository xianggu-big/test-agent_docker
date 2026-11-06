"""只读盘点：E9 失败语料分布（Arm 1 数据加工第一步）。零写入。"""
import json
import sqlite3

c = sqlite3.connect(r"file:d:/test agent_docker/data/test_agent.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
out = {}
out["tcr_schema"] = [r["name"] for r in c.execute(
    "PRAGMA table_info(test_case_results)")]
try:
    out["tcr_by_kind"] = [dict(r) for r in c.execute(
        """SELECT failure_kind, outcome, COUNT(*) n FROM test_case_results
           WHERE task_id>=418 GROUP BY failure_kind, outcome ORDER BY n DESC LIMIT 15""")]
except sqlite3.OperationalError as e:
    out["tcr_err"] = str(e)
out["rounds_stop"] = [dict(r) for r in c.execute(
    """SELECT stop_reason, COUNT(*) n FROM evaluation_rounds WHERE task_id>=418
       GROUP BY stop_reason ORDER BY n DESC LIMIT 10""")]
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
c.close()
