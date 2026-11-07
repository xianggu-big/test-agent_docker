"""php_adapter：PHPUnit junit XML → pytest 风格统一文本（白包阶段4，docs/多语言工具链接入任务包-20261106.md）。

S5 扩展枢纽：adapter 输出统一文本，下游 evaluate/repair/report 零改动（任务卡纪律）。
任务卡明文：PHPUnit --log-junit 与 surefire 同为 JUnit XML 家族——解析直接复用
case_results.parse_junit_xml（本模块只做文本渲染与覆盖表）。
- coverage：PHPUnit --coverage-clover XML（pcov 驱动）→ 文件级 statements/coveredstatements
  缺口行（B2 口径：全覆盖文件不进行列表；TOTAL 无条件输出——go 实锤教训）。
"""
from xml.etree import ElementTree as ET


def clover_to_rows(clover_text: str) -> tuple[list[dict], int]:
    """clover XML → (有缺口的文件级行, 总覆盖百分比)。B2 口径对齐 jacoco/coverprofile。"""
    try:
        root = ET.fromstring(clover_text or "")
    except ET.ParseError:
        return [], 0
    rows: list[dict] = []
    tot_s = tot_c = 0
    for f in root.iter("file"):
        metrics = f.find("metrics")
        if metrics is None:
            continue
        stmts = int(metrics.get("statements") or 0)
        covered = int(metrics.get("coveredstatements") or 0)
        tot_s += stmts
        tot_c += covered
        miss = stmts - covered
        if miss > 0:
            missing = [int(ln.get("num")) for ln in f.iter("line")
                       if ln.get("type") == "stmt" and ln.get("count") == "0"]
            name = (f.get("name") or "").replace("\\", "/")
            rows.append({"file": name, "miss": miss, "cover": covered,
                         "pct": round(100 * covered / stmts) if stmts else 0,
                         "missing": sorted(set(missing))})
    total = round(100 * tot_c / tot_s) if tot_s else 0
    return rows, total


def phpunit_junit_to_pytest_text(junit_text: str, clover_text: str | None = None) -> str:
    """PHPUnit junit → pytest 风格统一文本（计数/FAILED/ERROR 行/覆盖段）。

    evaluate 正则契约对齐：`(\\d+) passed` / `(\\d+) failed` / `TOTAL.*?\\s(\\d+)%`。"""
    from .case_results import parse_junit_xml
    rows = parse_junit_xml(junit_text or "")
    passed = sum(1 for r in rows if r["status"] == "passed")
    failed = sum(1 for r in rows if r["status"] == "failed")
    error = sum(1 for r in rows if r["status"] == "error")
    skipped = sum(1 for r in rows if r["status"] == "skipped")
    failed += error
    lines: list[str] = ["=========================== short test summary info ==========================="]
    for r in rows:
        if r["status"] == "failed":
            lines.append(f"FAILED {r['nodeid']} - {r['message']}")
        elif r["status"] == "error":
            lines.append(f"ERROR {r['nodeid']} - {r['message']}")
        elif r["status"] == "skipped":
            lines.append(f"SKIP {r['nodeid']} - {r['message']}")
    lines.append(f"=========================== {passed} passed, {failed} failed, "
                 f"{skipped} skipped ===========================")
    if clover_text:
        crows, total = clover_to_rows(clover_text)
        lines.append("---------- coverage: platform clover(pcov) ----------")
        lines.append(f"{'Name':<44}{'Miss':>6}{'Cover':>8}   Missing")
        for r in crows:
            lines.append(f"{r['file']:<44}{r['miss']:>6}{r['pct']:>7}%   "
                         f"{','.join(map(str, r['missing'][:40]))}")
        lines.append(f"{'TOTAL':<44}{'':>6}{total:>7}%")
    return "\n".join(lines) + "\n"
