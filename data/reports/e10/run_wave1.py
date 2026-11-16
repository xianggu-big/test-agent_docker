# -*- coding: utf-8 -*-
"""E10 Wave 1 小样校准波驱动器（~50 万 token 预算）。

三个校准实验（跑完出继续/缩量/砍 Arm 决策）：
  ①Arm 1 对照 5 格：E9 失败的 corpus-003 任务带陷阱库 v2 注入重跑
    （TRAPKB_V2_ENABLED=True 走 gen_tests 注入通道），对照组=E9 原格基线（零成本）；
  ②Arm 3 演进 1 序列 3 步：go-shop 副本演进 → 每步全 L1 接力（退化曲线可测性验证）；
  ③R1 烟测已单独完成（probe_r1_channel.py PASS，通道映射=flash+思考链）。

纪律：402 不降级（实验纯净性，设计 §3-2）/ 高峰门 / 每格 JSON 留档 / 两本账对账。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # run_wave1.py → e10 → reports → data → root
E10 = ROOT / "data" / "reports" / "e10"
OUT = E10 / "wave1_results.json"
RESULT_MARK = "@@RESULT@@"   # E9 worker 的留档标记（run_e9_full.py :363）


def pick_failed_cells(n: int = 5) -> list[dict]:
    """选 E9 failed 的 corpus-003 语料格（GT 已知：5 埋缺陷）——重跑可比基线。"""
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT t.id, p.name FROM test_tasks t JOIN projects p ON p.id=t.project_id
        WHERE p.name LIKE 'e9a1%corpus-003%' AND t.status='failed'
        ORDER BY t.id LIMIT ?""", (n,)).fetchall()
    c.close()
    return [{"tid": r["id"], "label": r["name"]} for r in rows]


def run_one_cell(cell: dict, idx: int, trapkb: bool) -> dict:
    """单格：构造 E9 worker params（flags 注入通道）→ 子进程跑 → 收结果。"""
    params = {
        "arm": "A1",
        "corpus": "corpus-003",
        "n": 900 + idx,                       # E10 序号（与 E9 的 1..64 错开）
        "combo_idx": 90 + idx,                # A1 label 必填（E9 的 1..64 错开）
        "flags": {"TRAPKB_V2_ENABLED": True} if trapkb else {},
        "profile": None,                      # 402 不降级（实验纯净性）
    }
    ppath = E10 / f"wave1_p{idx}.json"
    ppath.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "data" / "reports" / "e9_full" / "run_e9_full.py"),
             "--worker", str(ppath)],
            capture_output=True, text=True, timeout=45 * 60, cwd=str(ROOT))
        out = r.stdout or ""
        line = next((ln for ln in out.splitlines() if ln.startswith(RESULT_MARK)), None)
        rec = json.loads(line[len(RESULT_MARK):]) if line else {
            "status": "error", "error": (r.stderr or "no result mark")[-300:]}
    except subprocess.TimeoutExpired as e:
        rec = {"status": "error", "error": f"timeout: {(e.stdout or b'')[-200:]}"}
    except Exception as e:   # noqa: BLE001 —— 驱动器自诊断：异常类型入档
        rec = {"status": "driver_error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    rec["cell"] = cell["label"]
    rec["returncode"] = r.returncode if "r" in dir() else None
    rec["stdout_tail"] = (r.stdout or "")[-200:] if "r" in dir() else ""
    rec["stderr_tail"] = (r.stderr or "")[-200:] if "r" in dir() else ""
    rec["e9_baseline_tid"] = cell["tid"]
    rec["trapkb"] = trapkb
    rec["elapsed_s"] = int(time.time() - t0)
    # 402 不降级断言：budget_exhausted 即如实留档（本波预期内）
    return rec


def main() -> int:
    E10.mkdir(parents=True, exist_ok=True)
    cells = pick_failed_cells(5)
    print(f"Arm1 对照格（E9 failed corpus-003）: {[c['tid'] for c in cells]}", flush=True)
    results = []
    for i, cell in enumerate(cells):
        rec = run_one_cell(cell, i, trapkb=True)
        results.append(rec)
        print(f"[{i+1}/5] {rec.get('status')} verdict={rec.get('verdict')} "
              f"passed={rec.get('passed')} failed={rec.get('failed')} "
              f"tokens={rec.get('tokens_spent')} ({rec['elapsed_s']}s) "
              f"trapkb=ON", flush=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("落盘:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
