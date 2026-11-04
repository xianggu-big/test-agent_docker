"""基准真值拉取（11.4 恢复计划 §4 / Task H）：按 manifest 拉取 A 轨 buggy/fixed 两版本。

用法：
  python scripts/fetch_baseline.py <manifest.json> [--repos-dir data/benchmarks/repos]

诚实纪律（研究先行）：
- 不整体 vendor Defects4J/BugsInPy（体积/许可），按 manifest 逐项目 clone；
- 无网络/git 失败 → 明确 blocked（退出码 3），绝不伪造"已拉取"；
- B 轨（无真值冒烟）同样可拉，只是没有 fixed_ref。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from test_agent.skills.benchmarks import load_manifest  # noqa: E402


def _git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    return p.returncode == 0, (p.stdout or p.stderr or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="按 manifest 拉取基准项目")
    ap.add_argument("manifest")
    ap.add_argument("--repos-dir", default=str(_ROOT / "data" / "benchmarks" / "repos"))
    args = ap.parse_args()

    m, errs = load_manifest(Path(args.manifest))
    if errs:
        for e in errs:
            print(f"manifest 校验失败: {e}", file=sys.stderr)
        return 2

    repos = Path(args.repos_dir)
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{m['id']}.git"
    if bare.exists():
        print(f"[fetch] 已存在，跳过 clone: {bare}")
    else:
        ok, tail = _git(["clone", "--bare", m["repo"], str(bare)])
        if not ok:
            print(f"[fetch] blocked：clone 失败（无网络/仓库不可达）：{tail[-200:]}",
                  file=sys.stderr)
            return 3
    for label, key in (("buggy_ref", "buggy_ref"), ("fixed_ref", "fixed_ref")):
        ref = str(m.get(key) or "").strip()
        if not ref:
            print(f"[fetch] {label}: B 轨无真值，跳过")
            continue
        ok, tail = _git(["--git-dir", str(bare), "rev-parse", "--verify", ref])
        print(f"[fetch] {label}={ref}: {'OK' if ok else f'无法解析（{tail[-120]}'}）")
        if not ok:
            return 3
    print(f"[fetch] done: {bare}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
