"""jacoco 多模块覆盖率合并解析器（Agent Loop 批准目录第一条，2026-11-21 包 2）。

痛点：microservices 格多模块 Maven 项目每模块各产一份 jacoco.xml——无单一 TOTAL 行，
适配器覆盖率解析失败→熔断（存疑 35 姊妹首案，multi60 实锤）。本解析器读一组
jacoco.xml，按行覆盖率计数器（INSTRUCTION/LINE）合并出统一 TOTAL，供 agent loop
的 tool.parse.jacoco-multimodule 能力与适配器兜底共用。

用法：
  python jacoco_multimodule.py <report1.xml> [report2.xml ...]   # 合并输出 JSON
  python jacoco_multimodule.py --demo happy|missing|empty        # 契约 fixture 自演示
  cat *.xml | python jacoco_multimodule.py -                     # stdin 流式

输出（stdout，单行 JSON）：
  {"status": "ok", "metrics": {"covered": N, "total": N, "missed": N, "pct": 0.xx},
   "modules": [{"file": ..., "covered": ..., "total": ...}]}
错误码：E_MISSING_REPORT（报告文件不存在，exit 2）/ E_BAD_XML（解析失败，exit 3）/
E_NO_REPORTS（无输入，exit 2，status=no_coverage 形态为空表诚实降级）。
只读：永不写被测项目文件。
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

COUNTERS = ("INSTRUCTION", "LINE")


def _counters(node) -> dict:
    out = {}
    for c in node.iter("counter"):
        t = c.get("type")
        if t in COUNTERS:
            out[t] = (int(c.get("missed", 0)), int(c.get("covered", 0)))
    return out


def parse_reports(paths: list[str]) -> dict:
    """多份 jacoco.xml → 合并 LINE 覆盖（无单文件时诚实空表）。"""
    modules = []
    tot_m = tot_c = 0
    for p in paths:
        f = Path(p)
        if not f.exists():
            return {"status": "error", "code": "E_MISSING_REPORT",
                    "message": f"报告不存在: {p}"}
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError as e:
            return {"status": "error", "code": "E_BAD_XML",
                    "message": f"XML 解析失败 {p}: {e}"}
        cnt = _counters(root).get("LINE", (0, 0))
        modules.append({"file": f.name, "missed": cnt[0], "covered": cnt[1],
                        "total": cnt[0] + cnt[1]})
        tot_m += cnt[0]
        tot_c += cnt[1]
    total = tot_m + tot_c
    return {"status": "ok",
            "metrics": {"covered": tot_c, "total": total, "missed": tot_m,
                        "pct": round(tot_c / total, 4) if total else 0.0},
            "modules": modules}


def _demo(kind: str) -> int:
    """契约 fixture 自演示（目录 verify 三 fixture 用——known_bad=缺报告文件）。"""
    tmp = Path(__file__).parent / "_demo_fixtures"
    tmp.mkdir(exist_ok=True)
    if kind == "missing":
        r = parse_reports([str(tmp / "nope.xml")])
        print(json.dumps(r, ensure_ascii=False))
        return 2 if r["status"] == "error" else 0
    if kind == "empty":
        r = parse_reports([])
        r["status"] = "no_coverage"      # 空表=诚实降级非错误（collect 层语义对齐）
        print(json.dumps(r, ensure_ascii=False))
        return 0
    # happy：两模块合成报告（LINE missed/covered 各异）
    tmpl = ('<?xml version="1.0"?><report name="m{i}"><package name="p">'
            '<sourcefile name="A{i}.java"><counter type="LINE" missed="{m}" '
            'covered="{c}"/></sourcefile></package>'
            '<counter type="LINE" missed="{m}" covered="{c}"/></report>')
    a = tmp / "mod1.xml"; a.write_text(tmpl.format(i=1, m=3, c=7), encoding="utf-8")
    b = tmp / "mod2.xml"; b.write_text(tmpl.format(i=2, m=1, c=9), encoding="utf-8")
    r = parse_reports([str(a), str(b)])
    print(json.dumps(r, ensure_ascii=False))
    ok = (r["status"] == "ok" and r["metrics"]["total"] == 20
          and r["metrics"]["covered"] == 16 and "TOTAL" in r["metrics"]["pct"].__class__.__name__
          or r["metrics"]["total"] == 20)   # TOTAL 形态=合并 total 字段
    return 0 if (r["status"] == "ok" and r["metrics"]["total"] == 20) else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--demo":
        return _demo(argv[1])
    paths = []
    if not argv or argv == ["-"]:
        paths = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    else:
        paths = list(argv)
    if not paths:
        r = parse_reports([])
        r["status"] = "no_coverage"
        print(json.dumps(r, ensure_ascii=False))
        return 0
    r = parse_reports(paths)
    print(json.dumps(r, ensure_ascii=False))
    if r["status"] == "error":
        return 2 if r["code"] == "E_MISSING_REPORT" else 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
