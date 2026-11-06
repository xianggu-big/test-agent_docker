"""一次性修正：小样格 task420 实际执行的是全关组合（flags_of bug），移植到正确组合位 idx22。"""
import json
from pathlib import Path

OUT = Path(r"d:\test agent_docker\data\reports\e9_full")
src = OUT / "e9a1_corpus-001_00_n1.json"
rec = json.loads(src.read_text(encoding="utf-8"))
assert rec["task_id"] == 420 and all(v is False for v in rec["flags"].values()), rec
rec["combo_idx"] = 22
rec["note"] = "移植自 idx00 小样（flags_of 偏离量 bug 期实跑）；实际开关=全 False，与 flags 字段一致"
(OUT / "e9a1_corpus-001_22_n1.json").write_text(
    json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
src.unlink()
print("moved task420 -> idx22")
