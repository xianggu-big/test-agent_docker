"""今晚全模块质量检验（零 LLM，锚点以功能事实为准）"""
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
ok, fail = [], []
def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print(("✅ " if cond else "❌ ") + name + (f" | {str(detail)[:80]}" if not cond else ""))

for cell, tid in (("L2E1", 359), ("L2E2", 360), ("L2E3", 361)):
    d = json.loads(Path(f"data/reports/batch3/l2e_{cell}_t{tid}.json").read_text(encoding="utf-8"))
    check(f"T1 {cell}", d["task_id"] == tid and d["relay_real"] and d["rounds"])
m = json.loads(Path("data/reports/batch3/e7_metrics.json").read_text(encoding="utf-8"))["e7_hit_rate"]
check("T2 E7", m["mean"] == 0.5333 and m["std"] == 0.1155 and m["n"] == 3)
e8 = json.loads(Path("data/reports/batch3/e8_matrix.json").read_text(encoding="utf-8"))["e8_matrix"]
check("T3 E8", e8["l2_contract_violations"] == 6 and len(e8["legs"]) == 2)
t6 = Path("docs/论文草稿/第6章-系统测试与实验分析.md").read_text(encoding="utf-8")
check("T4 占位清零", t6.count("待跑数") == 0)
check("T4 新口径", "0.5333±0.1155" in t6 and "黑盒契约轨是集成层检出主力" in t6)
nodes_src = Path("test_agent/graph/nodes.py").read_text(encoding="utf-8")
check("N-349 内联配置", "注入最小内联配置" in nodes_src and "testEnvironment" in nodes_src)
check("N-348 gitignore", "_gi_ignore" in nodes_src)
check("N-变砖清扫", "_sweep_orphan_active_tasks" in Path("test_agent/api/server.py").read_text(encoding="utf-8"))
check("N-A3 D1", "d1_by_ai" in Path("test_agent/api/server.py").read_text(encoding="utf-8"))
check("N-run_grid", "run_task_levels" in Path("scripts/run_grid.py").read_text(encoding="utf-8"))
rb = Path("data/reports/batch3/run_batch3.py").read_text(encoding="utf-8")
check("N1 计数修复", "N1 修复" in rb and rb.count("subprocess.run(cmd") == 1)
print(f"===== {len(ok)} 过 / {len(fail)} 不过 =====")
sys.exit(1 if fail else 0)
