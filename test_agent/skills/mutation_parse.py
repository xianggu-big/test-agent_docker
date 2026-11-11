"""变异测试报告解析（任务卡-F 能力补齐，2026-11-12）：三语言变异报告 → 统一 schema。

统一口径（与 go_gremlins.py 同构）：{killed, survived, timeout, total, kill_rate,
score, survivors: [{mutator, location}]}。判据=变异报告（工具权威判定），
失败归类沿用 attribution 三分类（解析器失败=case_defect 域留痕，不新造 taxonomy）。

三工具报告格式（实测钉版本）：
- StrykerJS 8.5.x：`reports/mutation/json` → {mutationScore, statistics:{mutants:{killed,noCoverage,survived,...}}, files:{path:{mutants:[{mutatorName,status}]}}}
- PIT (pitest-maven)：target/pit-reports/mutations.xml（<mutation status='KILLED' …>）status ∈ KILLED/SURVIVED/TIMED_OUT/NO_COVERAGE/…
- Infection 0.29.x：infection.log 的 JSON 汇总（--log-verbosity=default + json 日志）→ {stats:{killed, escaped, errors, total, mutationScoreIndicator}}
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def _unified(killed: int, survived: int, timeout: int,
             survivors: list[dict]) -> dict:
    total = killed + survived + timeout
    return {
        "killed": killed, "survived": survived, "timeout": timeout,
        "total": total,
        "kill_rate": round(killed / total, 3) if total else None,
        "score": round((killed + timeout) / total * 100, 1) if total else None,
        "survivors": survivors[:10],
        "source": "mutation_report",
    }


def parse_stryker_json(report_path: str | Path) -> dict:
    """StrykerJS mutation/json 报告 → 统一 schema。文件不存在/不可解析 → 空表诚实降级。"""
    p = Path(report_path)
    if not p.is_file():
        return _unified(0, 0, 0, [])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return _unified(0, 0, 0, [])
    killed = survived = timeout = 0
    survivors: list[dict] = []
    for file_path, finfo in (data.get("files") or {}).items():
        for m in finfo.get("mutants") or []:
            st = m.get("status")
            if st == "Killed":
                killed += 1
            elif st == "Survived":
                survived += 1
                survivors.append({"mutator": m.get("mutatorName"),
                                  "location": f"{file_path}:{(m.get('location') or {}).get('start', {}).get('line', '?')}"})
            elif st == "Timeout":
                timeout += 1
    return _unified(killed, survived, timeout, survivors)


def parse_pit_xml(report_path: str | Path) -> dict:
    """PIT mutations.xml → 统一 schema。status ∈ KILLED/SURVIVED/TIMED_OUT/NO_COVERAGE。"""
    p = Path(report_path)
    if not p.is_file():
        return _unified(0, 0, 0, [])
    try:
        root = ET.parse(p).getroot()
    except (ET.ParseError, OSError):
        return _unified(0, 0, 0, [])
    killed = survived = timeout = 0
    survivors: list[dict] = []
    for m in root.iter("mutation"):
        st = (m.get("status") or "").upper()
        src = m.find("sourceFile")
        line = m.find("lineNumber")
        mutator = m.find("mutator")
        loc = f"{src.text if src is not None else '?'}:{line.text if line is not None else '?'}"
        if st == "KILLED":
            killed += 1
        elif st == "SURVIVED":
            survived += 1
            survivors.append({"mutator": (mutator.text if mutator is not None else "?"),
                              "location": loc})
        elif st in ("TIMED_OUT", "MEMORY_ERROR"):
            timeout += 1
    return _unified(killed, survived, timeout, survivors)


def parse_infection_json(report_path: str | Path) -> dict:
    """Infection json 日志 → 统一 schema。stats: killed/escaped/errors(含 timeout 域)
    + mutationScoreIndicator（百分比）。escaped=survived（同义不同名）。"""
    p = Path(report_path)
    if not p.is_file():
        return _unified(0, 0, 0, [])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return _unified(0, 0, 0, [])
    stats = data.get("stats") or {}
    killed = int(stats.get("killed") or 0)
    escaped = int(stats.get("escaped") or 0)
    errors = int(stats.get("errors") or 0) + int(stats.get("timed_out") or 0)
    survivors = []
    for m in (data.get("escaped_mutants") or [])[:10]:
        survivors.append({"mutator": m.get("mutator"),
                          "location": f"{m.get('originalFilePath', '?')}:{m.get('originalStartingLine', '?')}"})
    return {"killed": killed, "survived": escaped, "timeout": errors,
            "total": killed + escaped + errors,
            "kill_rate": round(killed / max(1, killed + escaped + errors), 3),
            "score": float(stats.get("mutationScoreIndicator") or 0),
            "survivors": survivors, "source": "mutation_report"}
