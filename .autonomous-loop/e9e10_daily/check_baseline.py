"""E9/E10 每日续跑：只读基线盘点（表清单/llm_calls 累计/exec_queue 状态）。零写入零 LLM。"""
import json
import sqlite3

DB = r"d:\test agent_docker\data\test_agent.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
out = {"tables": [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]}
if "llm_calls" in out["tables"]:
    out["llm_calls"] = [dict(r) for r in c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_tokens),0) AS total FROM llm_calls")]
for t in ("exec_queue", "tasks", "tasks_v2"):
    if t in out["tables"]:
        out[f"{t}_by_status"] = [dict(r) for r in c.execute(
            f"SELECT status, COUNT(*) AS n FROM {t} GROUP BY status ORDER BY n DESC")]
print(json.dumps(out, ensure_ascii=False, indent=1))
c.close()
