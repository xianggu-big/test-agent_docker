"""目录插件契约验证 CLI（2026-11-20 设计稿 §4.4；admin 人工工具——Agent 无入口）。

用法：
  python scripts/verify_plugin.py --list                        # 列目录状态
  python scripts/verify_plugin.py --fetch <catalog_id>          # 预拉取+digest 比对/封存
  python scripts/verify_plugin.py --verify <catalog_id> [--image mta-test:py312]
      # 三类 fixture 契约验证（scratch 容器；全过→人工可升 verified）
  python scripts/verify_plugin.py --set-status <catalog_id> <status>   # 推广/回滚/停用
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_agent.skills import plugin_manager as pm      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fetch")
    ap.add_argument("--verify")
    ap.add_argument("--image", default="mta-test:py312")
    ap.add_argument("--set-status", nargs=2, metavar=("CATALOG_ID", "STATUS"))
    args = ap.parse_args()

    if args.list:
        for t in pm.load_catalog():
            print(f"{t['id']:<32} v{t['version']:<10} {t['status']:<10} "
                  f"digest={'✓' if t.get('digest') else '未封存'} "
                  f"fixtures={len(t.get('fixtures') or [])}")
        return 0
    if args.fetch:
        entry = pm.find_entry(args.fetch)
        if entry is None:
            print("条目不存在"); return 2
        r = pm.fetch_artifact(entry)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        if r["ok"] and not r["sealed"]:
            print(f"→ 首次下载完成，主工件 sha256={r['digest']}\n"
                  f"→ 人工确认后把 digest 写入 catalog.json（封存=治理动作）")
        return 0 if r["ok"] else 1
    if args.verify:
        entry = pm.find_entry(args.verify)
        if entry is None:
            print("条目不存在"); return 2
        from test_agent.store.db import get_conn
        from test_agent.sandbox.runner import SandboxRunner
        import docker
        conn = get_conn()
        runner = SandboxRunner()
        cli = docker.from_env()
        container = cli.containers.run(args.image, detach=True, command="sleep 600",
                                       name=f"verify-plugin-{args.verify.replace('.', '-')}")
        try:
            # local-script 型：把脚本+fixtures 带进容器（容器内执行=隔离验证）
            src = (entry.get("source") or {})
            if src.get("type") == "local-script":
                host = pm.ROOT_SRC / src.get("path", "")
                runner.copy_dir_into(container, host.parent, keep_git=False)
            r = pm.verify_entry(conn, entry, runner, container)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            print(f"→ {r['detail']}")
            return 0 if r["ok"] else 1
        finally:
            container.remove(force=True)
    if args.set_status:
        from test_agent.store.db import get_conn
        r = pm.set_status(get_conn(), args.set_status[0], args.set_status[1])
        print(r["detail"])
        return 0 if r["ok"] else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
