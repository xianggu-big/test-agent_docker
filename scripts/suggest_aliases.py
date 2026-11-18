# -*- coding: utf-8 -*-
"""L3 失败语料自学习——别名候选挖掘器（2026-11-18 设计方案 §3，零 LLM 零网络）。

管线：读 data/reports/e10/trapkb_v2.json 失败聚类 → 反查簇内项目（真实库**只读**，
URI mode=ro）拿 repo_url → 内存构建项目索引（有持久索引文件则复用）→ 从簇内
top_messages 提取名词候选（只收真实出现在项目模块名/符号表里的）→ 产出
data/reports/e10/alias_suggestions.json 候选表。

人工流程（铁律：禁止运行时自动写表，防脏词污染路由）：
打开候选 JSON 逐条标 accept/reject → accept 条目由人手动补进
test_agent/skills/project_index.py 的 _ALIAS_GROUPS（一行一词，代码评审可见）→
同步重录 tests/alias_groups_snapshot.json（快照锁防无审漂移）。

用法：
    python scripts/suggest_aliases.py                       # 现网数据
    python scripts/suggest_aliases.py --trapkb X --out Y    # 指定路径（测试用）
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_agent.skills.project_index import build_index  # noqa: E402

DEFAULT_TRAPKB = ROOT / "data/reports/e10/trapkb_v2.json"
DEFAULT_OUT = ROOT / "data/reports/e10/alias_suggestions.json"
DEFAULT_DB = ROOT / "data/test_agent.db"
DEFAULT_INDEX_DIR = ROOT / "data/index"

# 消息通用词（断言样板/异常类/内建），不进候选
_STOPWORDS = {
    "assert", "assertion", "assertionerror", "error", "where", "true", "false",
    "none", "self", "int", "str", "len", "dict", "list", "type", "test",
    "failed", "passed", "raise", "return", "value", "object", "result",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _cluster_modules(proj_names: list[str], db_path: Path,
                     index_dir: Path) -> tuple[dict, dict, list[str]]:
    """簇内项目的模块聚合：返回 (modules{name: rec}, symbol2module, root_symbols)。

    模块清单来源优先持久索引文件（data/index/project_<pid>.json），缺则对 repo_url
    内存 build_index（只读扫描不落盘）。_root（根级平铺文件）符号单独归拢——
    闭包纪律排除 _root，但词候选仍可如实记录（suggested_module=None）。
    """
    mods: dict[str, dict] = defaultdict(lambda: {"files": [], "symbols": [], "loc": 0})
    symbol2module: dict[str, str] = {}
    root_symbols: set[str] = set()
    if not db_path.exists():
        return {}, {}, []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        seen: set[str] = set()
        for name in proj_names:
            if name in seen:
                continue
            seen.add(name)
            row = conn.execute("SELECT id, repo_url FROM projects WHERE name=? "
                               "ORDER BY id LIMIT 1", (name,)).fetchone()
            if not row:
                continue
            idx = None
            idx_file = index_dir / f"project_{row['id']}.json"
            if idx_file.exists():
                try:
                    idx = json.loads(idx_file.read_text(encoding="utf-8"))
                except Exception:      # noqa: BLE001 —— 坏索引=现场重建
                    idx = None
            repo = Path(row["repo_url"] or "")
            if (idx is None or not idx.get("modules")) and repo.is_dir():
                try:
                    idx = build_index(repo, ["python", "javascript", "go"])
                except Exception:      # noqa: BLE001 —— 索引失败=该项目跳过（如实）
                    continue
            if not idx:
                continue
            for m, rec in (idx.get("modules") or {}).items():
                tgt = mods[m]
                tgt["files"] = sorted(set(tgt["files"]) | set(rec.get("files", [])))
                tgt["symbols"] = sorted(set(tgt["symbols"]) | set(rec.get("symbols", [])))
                tgt["loc"] = tgt["loc"] + int(rec.get("loc") or 0)
                for s in rec.get("symbols", []):
                    if m != "_root":
                        symbol2module.setdefault(s, m)
                    else:
                        root_symbols.add(s)
    finally:
        conn.close()
    return dict(mods), symbol2module, sorted(root_symbols)


def _candidates(top_messages: list[str], mods: dict,
                symbol2module: dict, root_symbols: list[str]) -> list[dict]:
    """从簇消息提取名词候选：只收真实出现在模块名/符号表里的词（防脏词）。"""
    counts: dict[str, int] = defaultdict(int)
    for msg_text in top_messages:
        for w in set(_IDENT_RE.findall(msg_text or "")):
            lw = w.lower()
            if lw in _STOPWORDS:
                continue
            counts[lw] += 1
    out: list[dict] = []
    for w, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if w in mods:                                   # 词=模块名 → 直接绑定
            out.append({"suggested_alias": w, "suggested_module": w,
                        "evidence_n": n, "basis": "module_name"})
        elif w in symbol2module:                        # 词=某模块的符号 → 绑定该模块
            out.append({"suggested_alias": w, "suggested_module": symbol2module[w],
                        "evidence_n": n, "basis": "symbol_in_module"})
        elif w in root_symbols:                         # 根级符号 → 无模块归属（如实）
            out.append({"suggested_alias": w, "suggested_module": None,
                        "evidence_n": n,
                        "basis": "root_symbol（根级平铺项目，无模块可路由——仅词表参考）"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="L3 别名候选挖掘（人工审后进表）")
    ap.add_argument("--trapkb", default=str(DEFAULT_TRAPKB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--index-dir", default=str(DEFAULT_INDEX_DIR))
    ap.add_argument("--top", type=int, default=5, help="每簇最多保留候选数")
    args = ap.parse_args()

    trapkb = json.loads(Path(args.trapkb).read_text(encoding="utf-8"))
    traps = trapkb.get("traps") or []
    suggestions: list[dict] = []
    skipped_clusters: list[dict] = []
    for t in traps:
        proj_names = sorted(set(t.get("sample_proj") or []))
        mods, sym2mod, root_syms = _cluster_modules(
            proj_names, Path(args.db), Path(args.index_dir))
        cands = _candidates(t.get("top_messages") or [], mods, sym2mod, root_syms)
        cands = cands[: max(1, int(args.top))]
        if not cands:
            skipped_clusters.append({"signature": t.get("signature"),
                                     "size": t.get("size"),
                                     "reason": "簇消息无可识别符号候选或簇项目不可索引"})
            continue
        for c in cands:
            suggestions.append({"cluster_signature": t.get("signature"),
                                "size": t.get("size"),
                                "sample_proj": proj_names[:4], **c})
        # L3-2 护栏：模块绑定候选必须真实属于簇内项目模块清单
        for c in cands:
            assert c["suggested_module"] is None or c["suggested_module"] in mods, \
                f"模块绑定越界：{c}"

    out_doc = {
        "generated_at": time.strftime("%F %T"),
        "source": str(args.trapkb),
        "clusters_total": len(traps),
        "suggestions_total": len(suggestions),
        "manual_flow": "逐条标 accept/reject；accept 条目人工补进 "
                       "project_index._ALIAS_GROUPS 并重录 tests/alias_groups_snapshot.json",
        "suggestions": suggestions,
        "clusters_skipped": skipped_clusters,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[suggest_aliases] 簇 {len(traps)} → 候选 {len(suggestions)} 条 "
          f"（跳过簇 {len(skipped_clusters)}）→ {out_path}")
    for s in suggestions[:10]:
        print(f"  {s['cluster_signature']} size={s['size']} "
              f"alias={s['suggested_alias']} module={s['suggested_module']} "
              f"evidence={s['evidence_n']}")
    return 0 if suggestions else 1   # 零候选退出码 1（L3-1 判据：现网数据须 ≥1 条）


if __name__ == "__main__":
    raise SystemExit(main())
