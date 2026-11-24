# -*- coding: utf-8 -*-
"""T0-1 证据抢救归档：把 %TEMP% 里的实验原始账本复制到项目本地并出清单。

零 LLM。两个模式：
  --dry   只扫描并打印（目录数/库文件数/体积），不写任何东西
  --apply 真正复制 + 只读校验 + sha256 清单

归档规则（设计稿 T0-1）：
  al2x2_*        整库复制到 data/reports/agent_loop_2x2/tmpdb/<原目录名>/（含 -wal/-shm）
  bench3_* 等    只拷 *.db、结果 json、日志，不拷工作区和仓库克隆
  干跑总体积 > 500 MB 则停（写请示卡），不复制
源目录一律不删。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 前缀 → 归档策略
PREFIX_STRATEGIES = {
    # 整库复制（每个目录就是一个小 SQLite 库现场）
    "al2x2_": {"mode": "whole", "dest": ROOT / "data" / "reports" / "agent_loop_2x2" / "tmpdb"},
    # 只拷 db / 结果 json / 日志
    "bench3_": {"mode": "selective", "dest": ROOT / "data" / "reports" / "bench3_tmpdb"},
    "al_smoke_": {"mode": "selective", "dest": ROOT / "data" / "reports" / "al_smoke_tmpdb"},
    "bip_reverify_": {"mode": "selective", "dest": ROOT / "data" / "reports" / "bip_reverify_tmpdb"},
}

SELECTIVE_EXTS = {".db", ".json", ".log", ".txt"}
SELECTIVE_SUFFIXES = ("-wal", "-shm")  # 跟随同名 .db
LIMIT_BYTES = 500 * 1024 * 1024  # 干跑总体积上限 500 MB


def classify_tmp() -> dict:
    """扫描 %TEMP%，按前缀归类。返回 {prefix: [dir, ...]}。"""
    tmp = Path(tempfile.gettempdir())
    found: dict[str, list[Path]] = {}
    for p in sorted(tmp.iterdir()):
        if not p.is_dir():
            continue
        for prefix in PREFIX_STRATEGIES:
            if p.name.startswith(prefix):
                found.setdefault(prefix, []).append(p)
                break
    return found


def dir_stats(d: Path) -> tuple[int, int, int]:
    """返回 (文件数, 库文件数, 总字节)。"""
    n_files = n_db = 0
    total = 0
    for f in d.rglob("*"):
        if f.is_file():
            n_files += 1
            if f.suffix == ".db" or f.name.endswith(("-wal", "-shm")):
                n_db += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return n_files, n_db, total


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_whole(src: Path, dest_root: Path) -> list[Path]:
    """整目录复制（al2x2 库现场很小，直接树拷贝）。返回复制到的文件列表。"""
    dest = dest_root / src.name
    copied = []
    if dest.exists():
        # 幂等：已归档过就跳过（断点续跑不重拷）
        return []
    shutil.copytree(src, dest)
    copied.extend(p for p in dest.rglob("*") if p.is_file())
    return copied


def copy_selective(src: Path, dest_root: Path) -> list[Path]:
    """只拷 db/json/log/txt（bench3 的工作区与克隆不拷）。"""
    copied = []
    keep = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() in SELECTIVE_EXTS or any(
            f.name.endswith(sfx) for sfx in SELECTIVE_SUFFIXES
        ):
            keep.append(f)
    if not keep:
        return []
    rel_parts = src.name  # 平铺进 <dest>/<原目录名>/
    dest_dir = dest_root / rel_parts
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in keep:
        rel = f.relative_to(src)
        target = dest_dir / rel
        if target.exists():
            continue  # 幂等
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied.append(target)
    return copied


def verify_db(p: Path) -> dict:
    """只读打开校验一个 .db：可读 + 关键表行数。"""
    out: dict = {"path": str(p), "sha256": sha256_file(p)}
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            out["tables"] = tables
            counts = {}
            for t in tables:
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                except sqlite3.Error:
                    counts[t] = None
            out["row_counts"] = counts
        finally:
            conn.close()
        out["ok"] = True
    except sqlite3.Error as e:
        out["ok"] = False
        out["error"] = str(e)
    return out


def tmp_inventory(conn: sqlite3.Connection) -> dict:
    """临时库盘点表素材（给 T8）：agent LLM 行数、test.% 调用按 (name,status)、failed 归类。

    兼容两代 schema：
      llm_calls 相位分开数（agent_strategy / agent_diagnose / …）
      skill_invocations 新 schema 有 name 列；旧 schema 只有 skill_id，需 JOIN skills。
    """
    inv: dict = {}
    try:
        rows = conn.execute(
            "SELECT phase, COUNT(*) FROM llm_calls WHERE phase LIKE 'agent%' GROUP BY phase"
        ).fetchall()
        inv["agent_llm_by_phase"] = {r[0]: r[1] for r in rows}
        inv["agent_llm_rows"] = sum(r[1] for r in rows)
    except sqlite3.Error:
        inv["agent_llm_by_phase"] = None
        inv["agent_llm_rows"] = None
    si_cols = {c[1] for c in conn.execute("PRAGMA table_info(skill_invocations)").fetchall()}
    if "name" in si_cols:
        name_expr = "si.name"
        join = ""
    elif "skill_id" in si_cols:
        name_expr = "s.name"
        join = " JOIN skills s ON si.skill_id = s.id"
    else:
        join = None
    if join is not None:
        try:
            rows = conn.execute(
                f"SELECT {name_expr}, si.status, COUNT(*) FROM skill_invocations si{join} "
                f"WHERE {name_expr} LIKE 'test.%' GROUP BY {name_expr}, si.status"
            ).fetchall()
            inv["test_calls"] = [{"name": r[0], "status": r[1], "n": r[2]} for r in rows]
        except sqlite3.Error:
            inv["test_calls"] = None
        try:
            rows = conn.execute(
                f"SELECT {name_expr}, si.status, substr(COALESCE(si.error,''),1,200) "
                f"FROM skill_invocations si{join} "
                f"WHERE {name_expr} LIKE 'test.%' AND si.status='failed'"
            ).fetchall()
            inv["failed_samples"] = [
                {"name": r[0], "status": r[1], "error": r[2]} for r in rows
            ]
        except sqlite3.Error:
            inv["failed_samples"] = None
    else:
        inv["test_calls"] = None
        inv["failed_samples"] = None
    return inv


def main() -> int:
    dry = "--dry" in sys.argv
    found = classify_tmp()
    tmp = Path(tempfile.gettempdir())
    print(f"[archive] tempdir = {tmp}")
    print(f"[archive] Git Bash /tmp 与 Python tempdir 同目录: "
          f"{Path('/tmp').resolve() == tmp.resolve() if Path('/tmp').exists() else 'n/a'}")

    total_bytes = 0
    plan = {}
    for prefix, dirs in found.items():
        stats = [dir_stats(d) for d in dirs]
        n_files = sum(s[0] for s in stats)
        n_db = sum(s[1] for s in stats)
        nbytes = sum(s[2] for s in stats)
        total_bytes += nbytes
        plan[prefix] = {
            "dirs": [str(d) for d in dirs],
            "n_dirs": len(dirs),
            "n_files": n_files,
            "n_db": n_db,
            "bytes": nbytes,
            "strategy": PREFIX_STRATEGIES[prefix]["mode"],
            "dest": str(PREFIX_STRATEGIES[prefix]["dest"]),
        }
        print(
            f"[archive] {prefix:<14} dirs={len(dirs):>3} files={n_files:>5} "
            f"db={n_db:>3} size={nbytes/1048576:.1f}MB -> {plan[prefix]['strategy']}"
        )
    print(f"[archive] TOTAL {total_bytes/1048576:.1f} MB (limit {LIMIT_BYTES/1048576:.0f} MB)")

    if dry:
        return 0
    if total_bytes > LIMIT_BYTES:
        print(f"[archive] 干跑总体积超 500MB 上限，停（写请示卡），未复制任何东西")
        return 2

    manifest: dict = {"generated": "", "prefixes": {}, "dbs": []}
    from datetime import datetime, timezone

    manifest["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for prefix, info in plan.items():
        strategy = PREFIX_STRATEGIES[prefix]
        dest_root = strategy["dest"]
        dest_root.mkdir(parents=True, exist_ok=True)
        copied_files: list[str] = []
        verified_dbs: list[dict] = []
        for d in info["dirs"]:
            src = Path(d)
            if strategy["mode"] == "whole":
                copied_files.extend(str(p) for p in copy_whole(src, dest_root))
            else:
                copied_files.extend(str(p) for p in copy_selective(src, dest_root))
        # 只读校验所有归档后的 .db
        for f in sorted(dest_root.rglob("*.db")):
            v = verify_db(f)
            verified_dbs.append(v)
            # 顺手给每个库做临时库盘点（只对有 llm_calls 表的）
            if v.get("ok") and "llm_calls" in (v.get("tables") or []):
                try:
                    conn = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
                    try:
                        v["inventory"] = tmp_inventory(conn)
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass
        manifest["prefixes"][prefix] = {
            **info,
            "copied_n": len(copied_files),
            "verified_db_n": len(verified_dbs),
            "all_db_ok": all(v.get("ok") for v in verified_dbs) if verified_dbs else None,
        }
        manifest["dbs"].extend(verified_dbs)
        print(
            f"[archive] {prefix}: copied={len(copied_files)} dbs={len(verified_dbs)} "
            f"all_ok={manifest['prefixes'][prefix]['all_db_ok']}"
        )

    out = ROOT / "data" / "reports" / "tmp_ledger_archive_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[archive] manifest -> {out}")

    # 汇总盘点数字：设计稿的 128 / 73 专指 49 个 al2x2 库——按前缀分开汇总，不混算
    for prefix, info in manifest["prefixes"].items():
        # 用 dest 目录定位该前缀的库（path 含 tmpdb/<prefix 目标根>）
        dest_root = PREFIX_STRATEGIES[prefix]["dest"].as_posix()
        dbs = [d for d in manifest["dbs"] if d["path"].replace("\\", "/").startswith(dest_root)]
        phase_total: dict[str, int] = {}
        calls: dict[tuple, int] = {}
        unknown = 0
        for d in dbs:
            inv = d.get("inventory") or {}
            for ph, n in (inv.get("agent_llm_by_phase") or {}).items():
                phase_total[ph] = phase_total.get(ph, 0) + n
            if inv.get("test_calls") is None and inv.get("agent_llm_rows") is None:
                unknown += 1
            for c in inv.get("test_calls") or []:
                key = (c["name"], c["status"])
                calls[key] = calls.get(key, 0) + c["n"]
        strategy_n = phase_total.get("agent_strategy", 0)
        ok_n = sum(n for (nm, st), n in calls.items() if st == "ok")
        failed_n = sum(n for (nm, st), n in calls.items() if st == "failed")
        print(
            f"[archive] 盘点[{prefix}]: 库={len(dbs)} 无法读={unknown} "
            f"agent_strategy 行={strategy_n} 按相位={phase_total} "
            f"test.% 调用={ok_n + failed_n} (ok={ok_n} failed={failed_n})"
        )
        for (nm, st), n in sorted(calls.items()):
            print(f"[archive]   {nm} {st} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
