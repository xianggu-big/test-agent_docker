# -*- coding: utf-8 -*-
"""workspace 保留策略 dry-run（TODO-09）：只列清理清单，绝不删除——删除执行前必须人工请示。

策略（建议值，可参数化）：
  保留 ①最近 KEEP_RECENT 个 workspace ②task_id ≥ KEEP_MIN_TASK（近期实验可复现需要）
  其余列为待删（多为 2026-10-26~30 的冒烟/消融跑残留，exp-* tag 已冻结数据口径，
  原始 json/DB 均在库，workspace 目录可再生）。
用法：python scripts/workspace_cleanup.py [--keep-recent 12] [--keep-min-task 180] [--do-delete]
  无 --do-delete 即 dry-run。
"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "data" / "workspaces"


def main() -> int:
    args = sys.argv[1:]
    keep_recent = int(args[args.index("--keep-recent") + 1]) if "--keep-recent" in args else 12
    keep_min_task = int(args[args.index("--keep-min-task") + 1]) if "--keep-min-task" in args else 180
    do_delete = "--do-delete" in args
    if not WS.exists():
        print("无 workspaces 目录")
        return 0
    items = sorted(WS.iterdir(), key=lambda p: p.stat().st_mtime)
    keep, drop = [], []
    for p in items:
        try:
            tid = int(p.name.replace("task_", ""))
        except ValueError:
            keep.append(p)  # 非标准命名不动
            continue
        (keep if tid >= keep_min_task else drop).append(p)
    # 最近 N 个始终保留（在 drop 里的捞回来）
    for p in items[-keep_recent:]:
        if p in drop:
            drop.remove(p)
            keep.append(p)
    size = sum(f.stat().st_size for p in drop if p.is_dir() for f in p.rglob("*") if f.is_file())
    print(f"workspace 总数 {len(items)}｜保留 {len(keep)}｜待删 {len(drop)}（约 {size/1e6:.1f} MB）")
    print("待删清单（前 20）：", [p.name for p in drop[:20]])
    print("保留清单：", [p.name for p in keep][:15], "...")
    if do_delete:
        n = 0
        for p in drop:
            shutil.rmtree(p, ignore_errors=True)
            n += 1
        print(f"已删除 {n} 个")
    else:
        print("[dry-run] 未删除任何内容；确认后加 --do-delete 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
