# -*- coding: utf-8 -*-
"""T1 取证：httpie-bug2（+D 臂）逐用例转移矩阵精确分解 + pf 断言原文。

读 buggy 侧 t.db 的 test_case_results 与 fixed 侧 replay 的 fmap.json（容器内
产出），输出 fp/ff/pf/pp 分类与 pf 条目的断言源码行——判 A3（负空间声明诱导
过度严格断言/把 buggy 行为写进断言）是否成立。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path


def norm(n: str) -> str:
    return n.replace("\\", "/").replace(".py::", "::").replace(".py:", "::").strip()


def main() -> int:
    tmp = Path(sys.argv[1])            # bench3 tmp 区（含 t.db 与 workspaces/）
    fmap_raw = Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace")
    tid = int(sys.argv[3])
    m = re.search(r'\{"test_gen.*\}\s*$', fmap_raw, re.S | re.M)
    fmap = {norm(k): v for k, v in json.loads(m.group(0)).items()}

    conn = sqlite3.connect(str(tmp / "t.db"))
    conn.row_factory = sqlite3.Row
    b = {}
    for r in conn.execute("SELECT nodeid, status FROM test_case_results WHERE task_id=? "
                          "ORDER BY round_no, id", (tid,)):
        b[norm(r["nodeid"])] = r["status"]

    cats = {"fp": [], "ff": [], "pf": [], "pp": 0, "unmatched": 0}
    for n, bs in b.items():
        fs = fmap.get(n)
        if fs is None:
            cats["unmatched"] += 1
            continue
        bk = bs in ("failed", "error")
        if bk and fs == "passed":
            cats["fp"].append(n)
        elif bk and fs == "failed":
            cats["ff"].append(n)
        elif (not bk) and fs == "failed":
            cats["pf"].append(n)
        else:
            cats["pp"] += 1
    print(f"b_map={len(b)} f_map={len(fmap)} | pf={len(cats['pf'])} "
          f"ff={len(cats['ff'])} fp={len(cats['fp'])} pp={cats['pp']} "
          f"unmatched={cats['unmatched']}")

    # pf 条目的断言原文（函数名→源码段）
    ws = next((tmp / "workspaces").iterdir())
    src_by_file = {p.name: p.read_text(encoding="utf-8", errors="replace")
                   for p in ws.glob("test_gen_r*.py")}
    print("\n=== PF 条目（buggy 过、fixed 败）断言原文 ===")
    for n in cats["pf"]:
        fname, func = n.split("::", 1)[0] + ".py", n.split("::", 1)[1].split("[")[0]
        src = src_by_file.get(fname, "")
        mm = re.search(rf"def {re.escape(func)}\(.*?\n(?=\n|def |#)", src, re.S)
        print(f"\n--- {n} ---")
        print(mm.group(0)[:700] if mm else "(未定位)")

    print("\n=== FF 样本（buggy 败、fixed 败，前 6）===")
    for n in cats["ff"][:6]:
        print("  ", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
