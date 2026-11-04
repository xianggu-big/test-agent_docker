"""环境体检卡（11.4 恢复计划 §4 / Task H）：构建 + 官方测试套件全绿才允许进 agent 实验。

用法：
  python scripts/env_checkup.py <manifest.json>                    # 只校验+出 planned 卡
  python scripts/env_checkup.py <manifest.json> --exec --checkout <dir> [--out card.json]

诚实纪律：--exec 需要 checkout 目录里已经有项目源码（拉取走 fetch_baseline.py 或手工 clone）；
构建/测试任何一步失败 → status=failed，退出码 3——体检不过禁止进 agent 实验。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from test_agent.skills.benchmarks import health_card, load_manifest  # noqa: E402


def _run(cmd: str, cwd: Path, timeout_s: int) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout_s, encoding="utf-8",
                           errors="replace")
        return p.returncode == 0, (p.stdout or "")[-400:] + (p.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout_s}s）: {cmd}"
    except Exception as e:  # noqa: BLE001
        return False, f"执行失败: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="基准项目环境体检卡")
    ap.add_argument("manifest", help="manifest JSON 路径")
    ap.add_argument("--exec", action="store_true", help="实际执行构建+官方测试（需 --checkout）")
    ap.add_argument("--checkout", help="项目源码目录（已 clone/解压）")
    ap.add_argument("--out", help="体检卡 JSON 输出路径（缺省只打印）")
    ap.add_argument("--timeout", type=int, default=1800, help="单步超时秒数")
    args = ap.parse_args()

    m, errs = load_manifest(Path(args.manifest))
    if errs:
        print("manifest 校验失败：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2

    if not args.exec:
        card = health_card(m)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps(card, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        return 0

    if not args.checkout or not Path(args.checkout).is_dir():
        print("--exec 需要 --checkout 指向已就绪的项目源码目录", file=sys.stderr)
        return 2
    cwd = Path(args.checkout)
    t0 = time.monotonic()
    notes = []
    build_ok = True
    if str(m.get("setup_cmd") or "").strip():
        build_ok, tail = _run(m["setup_cmd"], cwd, args.timeout)
        notes.append(f"setup_cmd {'通过' if build_ok else '失败'}：{tail[-160:]}")
    tests_ok, tail = _run(m["official_test_cmd"], cwd, args.timeout)
    notes.append(f"official_test_cmd {'通过' if tests_ok else '失败'}：{tail[-160:]}")
    card = health_card(m, checkout=str(cwd), build_ok=build_ok, tests_ok=tests_ok,
                       duration_s=time.monotonic() - t0, notes="；".join(notes))
    print(json.dumps(card, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(card, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return 0 if card["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
