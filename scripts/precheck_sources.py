"""被测源码预算预检（通用版，T005）：对候选项目跑 _collect_sources_detailed，
报告系统实际会喂给 LLM 的源码预算占用——超限/切片/噪声排除在这里现形，零 LLM 零 Docker。

用法：
  python scripts/precheck_sources.py                     # 预检 data/candidates/ 下全部候选
  python scripts/precheck_sources.py path/to/repo [...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.skills.gen_tests import (  # noqa: E402
    MAX_SOURCE_CHARS, _collect_sources_detailed, _iter_source_files, WHOLE_FILE_LIMIT)

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "candidates"


def precheck(ws: Path, label: str | None = None) -> dict:
    label = label or ws.name
    srcs, skipped = _collect_sources_detailed(ws)
    total = sum(len(t) for _, t in srcs)
    all_files = list(_iter_source_files(ws))
    print(f"== {label}")
    print(f"   预算 {MAX_SOURCE_CHARS} | 整收阈值 {WHOLE_FILE_LIMIT} | 喂给 LLM {total} 字符"
          f"（{len(srcs)} 个条目）；源文件 {len(all_files)} 个，跳过台账 {len(skipped)} 条")
    for s, t in srcs[:8]:
        print(f"   [会喂] {s}  ({len(t)} 字符)")
    if len(srcs) > 8:
        print(f"   [会喂] ...共 {len(srcs)} 条")
    for s in skipped[:5]:
        print(f"   [跳过] {s}")
    # 结论：LLM 视野覆盖率（喂进字符 / 全部源文件字符）
    total_all = sum(len(t) for _, t in all_files)
    cover = round(total / total_all * 100, 1) if total_all else 0.0
    print(f"   视野覆盖率: {cover}%（{total}/{total_all} 字符）——>=90% 宜整项目入选；"
          f"60-90% 靠切片核心可见可用；<60% 不宜入 E6")
    return {"label": label, "fed_chars": total, "total_chars": total_all,
            "coverage_pct": cover, "entries": len(srcs), "skipped": len(skipped)}


def main() -> int:
    args = [Path(a) for a in sys.argv[1:]]
    targets = [(p, p.name) for p in args] if args else [
        (d, d.name) for d in sorted(CANDIDATES.iterdir()) if d.is_dir()]
    if not targets:
        print(f"无候选（{CANDIDATES} 为空）。先下载："
              f"curl --noproxy \"*\" -L https://codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/main")
        return 2
    rows = [precheck(ws, label) for ws, label in targets]
    print("\n== 选型建议（E6 入选线：视野覆盖率>=60% 且有真实业务逻辑） ==")
    for r in sorted(rows, key=lambda x: -x["coverage_pct"]):
        verdict = "入选" if r["coverage_pct"] >= 60 else "不宜"
        print(f"  {r['label']}: {r['coverage_pct']}% → {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
