"""java_adapter：surefire XML → pytest 风格统一文本（白包阶段2，docs/多语言工具链接入任务包-20261106.md）。

S5 扩展枢纽：adapter 输出统一文本，下游 evaluate/repair/report 零改动（任务卡纪律）。
- surefire XML 与 pytest --junitxml 同 family（testsuite/testcase/failure/error），
  逐用例结果落库复用 case_results.parse_junit_xml；本模块负责目录扫描与文本渲染。
- JaCoCo XML（jacoco:report）→ term-missing 覆盖表：只取有缺口的类级 LINE 行（B2 口径对齐，
  全覆盖类不进——与 per_file「无缺口证据=覆盖」一致）。
- 堆栈 'at ...' 行保留进统一文本（归因三闸消费堆栈首帧，S6）。
"""
import math
import xml.etree.ElementTree as ET
from pathlib import Path


def _parse_suite(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    suite = {"name": root.get("name", ""), "cases": []}
    for tc in root.iter("testcase"):
        failure = tc.find("failure")
        error = tc.find("error")
        status = "passed"
        detail, kind = "", ""
        if failure is not None:
            status, kind = "failed", "failure"
            detail = (failure.get("message") or "") + "\n" + (failure.text or "")
        elif error is not None:
            status, kind = "error", "error"
            detail = (error.get("message") or "") + "\n" + (error.text or "")
        suite["cases"].append({"name": tc.get("name", ""),
                               "classname": (tc.get("classname") or "").split(".")[-1],
                               "status": status, "kind": kind, "detail": detail.strip()})
    return suite


def _stack_lines(detail: str, limit: int = 3) -> list[str]:
    """取 'at ...' 堆栈行（前 limit 条）——归因消费堆栈首帧。"""
    return [ln.strip() for ln in detail.splitlines() if ln.strip().startswith("at ")][:limit]


def surefire_dir_to_pytest_text(report_dir: str | Path, jacoco_xml: str | Path | None = None) -> str:
    """surefire-reports 目录 → pytest 风格文本（计数/FAILED/ERROR 行/覆盖段）。

    stdout 非 JSON 的 java 版等价物：run 节点把 mvn test 的产物转成本文本交给下游。"""
    d = Path(report_dir)
    suites = [_parse_suite(p) for p in sorted(d.glob("TEST-*.xml"))]
    passed = failed = errors = 0
    body: list[str] = []
    stack_lines: list[str] = []
    for s in suites:
        for c in s["cases"]:
            if c["status"] == "passed":
                passed += 1
            elif c["status"] == "failed":
                failed += 1
                body.append(f"FAILED {c['classname']}::{c['name']} - "
                            f"AssertionError: {c['detail'].splitlines()[0] if c['detail'] else ''}")
            else:
                errors += 1
                body.append(f"ERROR {c['classname']}::{c['name']} - "
                            f"{c['detail'].splitlines()[0] if c['detail'] else ''}")
            stack_lines.extend(_stack_lines(c["detail"]))
    lines: list[str] = ["=========================== short test summary info ==========================="]
    lines.extend(body)
    lines.append(f"=========================== {passed} passed, {failed} failed, "
                 f"{errors} error ===========================")
    if stack_lines:
        lines.append("---------- java stack (top frames) ----------")
        lines.extend(stack_lines)
    if jacoco_xml is not None and Path(jacoco_xml).exists():
        rows, total = jacoco_xml_to_rows(Path(jacoco_xml).read_text(encoding="utf-8",
                                                                      errors="replace"))
        if rows:
            lines.append("---------- coverage: platform jacoco ----------")
            lines.append(f"{'Name':<44}{'Miss':>6}{'Cover':>8}   Missing")
            for r in rows:
                lines.append(f"{r['file']:<44}{r['miss']:>6}{r['pct']:>7}%   "
                             f"{','.join(map(str, r['missing']))}")
            lines.append(f"{'TOTAL':<44}{'':>6}{total:>7}%")
    return "\n".join(lines) + "\n"


def jacoco_xml_to_rows(xml_text: str) -> tuple[list[dict], int]:
    """JaCoCo XML → (有缺口的类级 LINE 行, 总覆盖百分比)。B2 口径：全覆盖类不进行列表。"""
    root = ET.fromstring(xml_text)
    rows: list[dict] = []
    tot_missed = tot_covered = 0
    for cls in root.iter("class"):
        for cnt in cls.findall("counter"):
            if cnt.get("type") != "LINE":
                continue
            missed, covered = int(cnt.get("missed", 0)), int(cnt.get("covered", 0))
            tot_missed += missed
            tot_covered += covered
            if missed > 0:
                src = cls.get("sourcefilename") or (cls.get("name") or "").split("/")[-1]
                # JaCoCo XML 无行号清单（batoo 方法级才有）——缺行列表留空，类级缺口数可审计
                rows.append({"file": src, "miss": missed, "cover": covered,
                             "pct": round(100 * covered / (missed + covered)) if (missed + covered) else 0,
                             "missing": []})
                break                       # 每类只取 LINE counter 一个
    total_pct = round(100 * tot_covered / (tot_missed + tot_covered)) if (tot_missed + tot_covered) else 0
    return rows, total_pct
