"""盲测语料构建框架 —— 三方盲测协议的"出题人"产物校验与登记（docs/链路方法测试文档.md §2）。

用法：
  python scripts/build_corpus.py validate data/corpus/corpus-001   # 校验结构/schema/可导入性
  python scripts/build_corpus.py list                              # 列出全部语料

校验纪律：ground_truth.json 不在 project/ 内（防泄题）；project 可被 import；
每个 planted_defect 必含 detectable_by 分层字段。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "data" / "corpus"

REQUIRED_KEYS = {"corpus_id", "theme", "requirement_summary", "planted_defects"}
DEFECT_KEYS = {"id", "type", "location", "description", "detectable_by"}


def validate(corpus_path: Path) -> list[str]:
    errs = []
    gt = corpus_path / "ground_truth.json"
    proj = corpus_path / "project"
    if not gt.exists():
        return [f"缺 ground_truth.json: {gt}"]
    data = json.loads(gt.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - set(data)
    if missing:
        errs.append(f"answer key 缺字段: {missing}")
    for d in data.get("planted_defects", []):
        if not DEFECT_KEYS <= set(d):
            errs.append(f"{d.get('id')}: 缺 {DEFECT_KEYS - set(d)}")
        if d.get("detectable_by") not in ("assertion", "integration", "performance"):
            errs.append(f"{d.get('id')}: detectable_by 非法")
    if (proj / "ground_truth.json").exists():
        errs.append("泄题：ground_truth.json 出现在 project/ 内（答题人会看到）")
    if not list(proj.glob("*.py")):
        errs.append("project/ 无 .py 文件")
    sys.path.insert(0, str(proj))
    for py in proj.glob("*.py"):
        if py.name.startswith("test_") or py.name == "REQUIREMENT.md":
            continue
        compile(py.read_text(encoding="utf-8"), py.name, "exec")
    sys.path.pop(0)
    return errs


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "validate":
        target = Path(sys.argv[2])
        errs = validate(target)
        print("\n".join(errs) if errs else f"OK {target.name}")
        return 1 if errs else 0
    for d in sorted(CORPUS_DIR.glob("corpus-*")):
        errs = validate(d)
        print(f"{d.name}: {'OK' if not errs else 'ERR ' + '; '.join(errs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
