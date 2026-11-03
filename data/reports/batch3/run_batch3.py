"""批 3：E6-E8 跑数回填（必做项工作包，后台执行）。
顺序：L2E1/L2E2/L2E3 三格 × corpus-003（n=1 先行探路，格间串行）→ E8 矩阵 → E7 n=3。
停车线：总 token 超 250k 中止（余额 ~8.68 元预算内）。结果落 data/reports/batch3/。
断点续跑：已存在的 JSON 不重跑。"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\test agent_docker")
OUT = Path(r"D:\test agent_docker\data\reports\batch3")
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "run.log"

def tokens():
    sys.path.insert(0, r"D:\test agent_docker")
    from test_agent.store.db import get_conn
    conn = get_conn()
    n = conn.execute("SELECT COALESCE(SUM(total_tokens),0) AS t FROM llm_calls").fetchone()["t"]
    conn.close()
    return n

def run(cmd, name):
    out = OUT / f"{name}.json"
    if out.exists():
        print(f"[skip] {name} 已存在")
        return
    print(f"[run] {name}: {cmd}", flush=True)
    t0 = tokens()
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600,
                       cwd=r"D:\test agent_docker")
    (OUT / f"{name}.stdout.txt").write_text((r.stdout or "")[-3000:], encoding="utf-8")
    spent = tokens() - t0    # N1 修复：不再跑两遍 subprocess；stdout 落档取自同一次执行
    out.write_text(json.dumps({"cmd": cmd, "tokens_spent": spent, "at": time.strftime("%F %T")},
                              ensure_ascii=False), encoding="utf-8")
    print(f"[done] {name} spent={spent}", flush=True)

def main():
    t0 = tokens()
    print(f"[start] tokens baseline={t0}", flush=True)
    # E6-E8a：L2E 三格（走 levels 接力，每格一个任务）
    for cell in ("L2E1", "L2E2", "L2E3"):
        run(f'python scripts/run_grid.py {cell} corpus-003', f"grid_{cell}")
        if tokens() - t0 > 250_000:
            print("[budget] 停车线触顶，中止后续", flush=True)
            return
    # E6-E8b：E8 矩阵
    run("python scripts/run_e8_levels.py --target corpus-003", "e8_matrix")
    # E6-E8c：E7 n=3
    run("python scripts/run_e7_corpus003.py --n 3", "e7_n3")
    print(f"[finish] total spent={tokens()-t0} (≈{(tokens()-t0)/144000:.2f} 元)", flush=True)

if __name__ == "__main__":
    main()
