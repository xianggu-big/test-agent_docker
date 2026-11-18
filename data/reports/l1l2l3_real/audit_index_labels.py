# -*- coding: utf-8 -*-
"""索引标签质量审计（2026-11-19，用户问"标签贴得如何——标注覆盖率+标签正确率"）。

三维度（全确定性零 LLM，真实库/工作区只读）：
  A. 文件标注覆盖率：索引 modules[].files ∪ vs 真实工作区源码文件
     （排除生成用例 test_gen_*/test_imported 与非代码清单）——"标签贴全了没"
  B. 符号正确率抽验：索引 symbols 每项目抽 ≤10 个，回原文件 grep 确认
     （防幻觉标注）——"贴的标签是真的吗"
  C. 端点正确率对账：索引 endpoints vs 源码复扫（openapi 解析+HTTP 正则同款）
     ——"端点清单可信吗"
  D. 模块切分合理性：索引模块名 vs 真实目录名对账——"边界贴对了吗"
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data/reports/l1l2l3_real/label_audit_results.json"
CODE_EXTS = {".py", ".js", ".ts", ".go", ".java", ".php"}
GEN_PREFIX = ("test_gen_", "test_imported", "TestGen", "test_")   # 生成物/测试不进"真实源码"口径


def real_source_files(ws: Path) -> list:
    """与 build_index._walk_src 同口径（可测源码）：排除测试目录/测试文件命名——
    否则"标注覆盖率"被审计器自己的测试文件打孔（口径打架）。"""
    out = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in CODE_EXTS:
            continue
        rel = p.relative_to(ws).as_posix()
        stem = Path(rel).stem
        if (Path(rel).name.startswith(("test_", "TestGen"))
                or stem.endswith("_test") or stem.endswith(".spec") or stem.endswith(".test")):
            continue
        if any(part.lower() in ("test", "tests", "spec", "__tests__", "e2e", "spec_unit")
               for part in Path(rel).parts):
            continue
        if any(part in (".git", "__pycache__", "node_modules", "vendor", "venv")
               for part in Path(rel).parts):
            continue
        out.append(rel)
    return out


def audit_task(tid: int) -> dict:
    idx_file = ROOT / f"data/index/project_{tid}.json"
    ws = ROOT / f"data/workspaces/task_{tid}"
    rec = {"tid": tid, "index_exists": idx_file.exists(), "ws_exists": ws.is_dir()}
    if not (idx_file.exists() and ws.is_dir()):
        return rec
    # force 重建（2026-11-19 标签修复后重审计：测试文件排除+maven 段排除）
    from test_agent.skills.project_index import ensure_index
    idx = ensure_index(tid, ws, ["python"], force=True)
    mods = idx.get("modules") or {}
    labeled = {f for m in mods.values() for f in m.get("files", [])}
    real = real_source_files(ws)

    # A. 文件标注覆盖率
    unlabeled = sorted(set(real) - labeled)
    over = sorted(labeled - set(real))   # 标了但文件不存在=脏标注
    rec["files"] = {"real_n": len(real), "labeled_n": len(labeled & set(real)),
                    "coverage_pct": round(len(labeled & set(real)) / len(real) * 100, 1) if real else None,
                    "unlabeled": unlabeled[:12], "unlabeled_n": len(unlabeled),
                    "dirty_over": over[:8], "dirty_n": len(over)}

    # B. 符号正确率抽验（每模块抽 ≤5 个，在模块**全部文件**里 grep——符号是模块级聚合）
    checked, ok = 0, 0
    bad_sym = []
    for mname, mrec in mods.items():
        syms = [s for s in (mrec.get("symbols") or []) if s][:5]
        srcs = []
        for f in mrec.get("files", []):
            fp = ws / f
            if fp.exists():
                srcs.append(fp.read_text(encoding="utf-8", errors="ignore"))
        blob = "\n".join(srcs)
        for sym in syms:
            if not blob:
                continue
            checked += 1
            if re.search(rf"\b{re.escape(sym)}\b", blob):
                ok += 1
            else:
                bad_sym.append(f"{mname}:{sym}")
    rec["symbols"] = {"checked": checked, "real": ok,
                      "precision_pct": round(ok / checked * 100, 1) if checked else None,
                      "bad": bad_sym[:10]}

    # C. 端点对账（复扫源码：openapi + express/PHP 正则 + go 路由同 build_index 口径）
    eps = idx.get("endpoints") or []
    ep_paths_src = set()
    try:
        from test_agent.skills.endpoint_extract import parse_go_routes
    except Exception:
        parse_go_routes = None
    for f in real:
        fp = ws / f
        text = fp.read_text(encoding="utf-8", errors="ignore")[:200000]
        if f.lower().endswith(".json") and "openapi" in f.lower():
            try:
                spec = json.loads(text)
                for p in (spec.get("paths") or {}):
                    ep_paths_src.add(p)
            except Exception:
                pass
        if f.lower().endswith(".go") and parse_go_routes:
            for ep in parse_go_routes(text):
                ep_paths_src.add(ep.get("path"))
        for m in re.finditer(
                r"""(?:app|router|\$app|\$router)->?(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]""", text, re.I):
            ep_paths_src.add(m.group(1))
    ep_idx_paths = {e.get("path") for e in eps}
    rec["endpoints"] = {"indexed_n": len(eps), "src_paths": sorted(ep_paths_src)[:12],
                        "src_n": len(ep_paths_src),
                        "covered": len(ep_idx_paths & ep_paths_src),
                        "missed": sorted(ep_paths_src - ep_idx_paths)[:10],
                        "hallucinated": sorted(ep_idx_paths - ep_paths_src)[:10]}

    # D. 模块切分合理性：索引模块名应对应真实**路径段**（深层布局如 app/auth 也算命中）
    real_segs = set()
    for p in ws.rglob("*"):
        parts = [s.lower() for s in p.relative_to(ws).parts[:-1]
                 if s.lower() not in ("src", "lib", "main", "app", "api", "java",
                                      "com", "org", "net", "target",
                                      ".git", "__pycache__", "node_modules", "vendor", "venv")]
        real_segs.update(parts)
    mod_names = {m for m in mods if m != "_root"}
    rec["modules"] = {"indexed": sorted(mod_names), "real_dirs": sorted(real_segs)[:14],
                      "match_pct": round(len(mod_names & real_segs) / len(mod_names) * 100, 1)
                      if mod_names else None}
    return rec


def main() -> int:
    tids = list(range(1966, 1982))   # 今晚全部 16 任务（bugverify 6 + E2E 3 + L3 1 + 矩阵 6）
    rows = [audit_task(t) for t in tids]
    done = [r for r in rows if r.get("files")]
    agg = {
        "generated_at": time.strftime("%F %T"),
        "tasks_audited": len(done),
        "file_coverage_avg_pct": round(sum(r["files"]["coverage_pct"] or 0 for r in done)
                                       / len(done), 1) if done else None,
        "symbol_precision_avg_pct": (lambda v: round(sum(v) / len(v), 1) if v else None)(
            [r["symbols"]["precision_pct"] for r in done if r["symbols"]["precision_pct"] is not None]),
        "rows": rows,
    }
    OUT.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"审计 {len(done)}/{len(rows)} 任务（有索引+有工作区）")
    print(f"文件标注覆盖率均值: {agg['file_coverage_avg_pct']}%")
    print(f"符号正确率均值: {agg['symbol_precision_avg_pct']}%")
    for r in rows:
        if not r.get("files"):
            print(f"  #{r['tid']}: 跳过（索引或工作区缺失）")
            continue
        f, s, e, m = r["files"], r["symbols"], r["endpoints"], r["modules"]
        print(f"  #{r['tid']}: 文件标注 {f['labeled_n']}/{f['real_n']}={f['coverage_pct']}% "
              f"脏标={f['dirty_n']} 未标={f['unlabeled_n']} | 符号 {s['real']}/{s['checked']}="
              f"{s['precision_pct']}% | 端点 索引{e['indexed_n']}/源码{e['src_n']} "
              f"漏={len(e['missed'])} 幻={len(e['hallucinated'])} | 模块匹配 {m['match_pct']}% {m['indexed']}")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
