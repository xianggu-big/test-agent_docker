# -*- coding: utf-8 -*-
"""B2 项目归一迁移：写实 repo_key + 合并重复项目行（test_tasks.project_id 迁到最早创建行）。

用法（在仓库根目录运行）：
  python scripts/merge_projects.py            # dry-run（只统计，不动库）
  python scripts/merge_projects.py --apply    # 先自动备份 .db 再执行
  python scripts/merge_projects.py --apply --no-backup   # 已有备份时跳过

安全：--apply 默认把 data/test_agent.db 备份为 data/test_agent.db.bak_merge_projects_<ts>；
合并只动 projects / test_tasks.project_id 两个位置，其余表（含 test_scripts 等按 task_id 关联）不受影响。
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config                      # noqa: E402
from test_agent.store.db import get_conn, init_db  # noqa: E402
from test_agent.store.projects import backfill_project_keys  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（已有备份时用）")
    args = ap.parse_args()

    db = Path(config.DB_PATH)
    if not db.exists():
        print(f"数据库不存在：{db}")
        return 1
    if args.apply and not args.no_backup:
        bak = db.with_name(f"{db.name}.bak_merge_projects_{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(db, bak)
        print(f"已备份：{bak}")

    init_db(db)          # 确保 repo_key 列已补（老库 ALTER）
    conn = get_conn(db)
    out = backfill_project_keys(conn, dry_run=not args.apply)
    conn.close()

    print(f"\n{'[dry-run] ' if out['dry_run'] else '[applied] '}项目分组 {out['groups']} 个；"
          f"重复行 {out['merged']} 个；涉及任务 {out['tasks_moved']} 个")
    if out["detail"]:
        print("\n合并明细（保留 id ← 合并掉 id）：")
        for d in out["detail"][:30]:
            print(f"  {d['name'][:34]:36s} keep={d['keep']:<4} drop={d['dup_ids']}")
        if len(out["detail"]) > 30:
            print(f"  … 另 {len(out['detail']) - 30} 组")
    if not args.apply:
        print("\n这是 dry-run，未修改数据库。确认后执行：python scripts/merge_projects.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
