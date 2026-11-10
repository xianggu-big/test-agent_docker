"""CoverageAdapter 统一覆盖率抽象（任务卡-F2 阶段 0，2026-11-10）。

多语言全矩阵的"统一层先行"：5 个后端（coverage.py JSON / istanbul coverageMap /
JaCoCo XML / go coverprofile / clover XML）→ 同一 schema——

    {"source": …, "files": [{file, statements, missing_lines, branches_covered,
                             branches_total}], "totals": {statements, missing}}

per_file 字段集五后端逐字一致（单测锁）；格式缺分支信息时 branches_* 如实
None（不编 0）。既有各语言适配器的 term-missing 文本渲染路径不动（字节兼容
零回归）；本层是跨语言数据通道：C3 缺口回灌（gap_lines）与统一 per_file 报告
的直接数据源，阶段 1+ 的语言补全一律走这里，不再各写一遍解析。

解析纪律：纯函数、零 IO 零 LLM；格式不合法返回 {"files": [], "totals": 0}
（诚实空，不抛——上层报告路径常拿不到覆盖率产物）。
"""
import re
import xml.etree.ElementTree as ET

FILE_KEYS = ("file", "statements", "missing_lines", "branches_covered", "branches_total")


def _doc(source: str, files: list[dict]) -> dict:
    tot_s = sum(f["statements"] for f in files)
    tot_m = sum(len(f["missing_lines"]) for f in files)
    return {"source": source, "files": files,
            "totals": {"statements": tot_s, "missing": tot_m}}


def compress_ranges(lines: list[int]) -> str:
    """缺口行压缩：[14,15,20,21,22,30] → "14-15, 20-22, 30"（C3 提示词口径）。"""
    out: list[str] = []
    start = prev = None
    for n in sorted(set(lines)):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev > start else f"{start}")
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ", ".join(out)


def gap_lines(doc: dict, max_files: int = 6, max_ranges: int = 6) -> list[str]:
    """C3 缺口回灌：统一文档 → 提示词行（缺口文件按缺口数降序）。"""
    files = sorted(doc.get("files") or [], key=lambda f: -len(f["missing_lines"]))
    out = []
    for f in files[:max_files]:
        if not f["missing_lines"]:
            continue
        ranges = compress_ranges(f["missing_lines"])
        if len(ranges) > 160:
            ranges = ", ".join(ranges.split(", ")[:max_ranges]) + " …"
        out.append(f"{f['file']}: 未覆盖行 {ranges}（共 {len(f['missing_lines'])} 行）")
    return out


# —————————————————————————— coverage.py JSON（pytest --cov-report=json） ——————————————————————————

def from_coverage_py_json(data: dict) -> dict:
    files = []
    for path, info in sorted((data.get("files") or {}).items()):
        s = info.get("summary") or {}
        files.append({"file": path.replace("\\", "/"),
                      "statements": int(s.get("num_statements") or 0),
                      "missing_lines": sorted(int(x) for x in info.get("missing_lines") or []),
                      "branches_covered": (int(s["covered_branches"])
                                           if s.get("num_branches") is not None else None),
                      "branches_total": (int(s["num_branches"])
                                         if s.get("num_branches") is not None else None)})
    return _doc("coverage.py", files)


# —————————————————————————— istanbul coverageMap（jest --coverageReporters=json） ——————————————————————————

def from_istanbul(coverage_map: dict, root: str = "") -> dict:
    files = []
    root_norm = root.replace("\\", "/").rstrip("/") + "/" if root else ""
    for _path, entry in sorted((coverage_map or {}).items()):
        smap = entry.get("statementMap") or {}
        counts = entry.get("s") or {}
        missing: set[int] = set()
        statements = 0
        for key, loc in smap.items():
            statements += 1
            if not counts.get(key):
                missing.add(int(loc.get("start", {}).get("line") or 0))
        bmap = entry.get("branchMap") or {}
        bcounts = entry.get("b") or {}
        b_tot = b_cov = 0
        has_b = bool(bmap)
        for key, locs in bmap.items():
            per = bcounts.get(key) or []
            b_tot += len(locs.get("locations") or per or [])
            b_cov += sum(1 for v in per if v)
        file_key = entry.get("path") or _path
        rel = file_key.replace("\\", "/")
        if root_norm and rel.startswith(root_norm):
            rel = rel[len(root_norm):]
        files.append({"file": rel,
                      "statements": statements,
                      "missing_lines": sorted(missing - {0}),
                      "branches_covered": b_cov if has_b else None,
                      "branches_total": b_tot if has_b else None})
    return _doc("istanbul", files)


# —————————————————————————— JaCoCo XML（mvn jacoco:report） ——————————————————————————

def from_jacoco_xml(xml_text: str) -> dict:
    """sourcefile 级（有行号——比既有 java_adapter 类级更细，C3 需要）。"""
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return _doc("jacoco", [])
    files = []
    for pkg in root.iter("package"):
        pname = (pkg.get("name") or "").replace("/", "/")
        for sf in pkg.iter("sourcefile"):
            missing: list[int] = []
            stmts = 0
            mb = cb = 0
            for ln in sf.iter("line"):
                mi, ci = int(ln.get("mi") or 0), int(ln.get("ci") or 0)
                if mi + ci > 0:
                    stmts += 1
                if ci == 0 and mi > 0:
                    missing.append(int(ln.get("nr") or 0))
                mb += int(ln.get("mb") or 0)
                cb += int(ln.get("cb") or 0)
            files.append({"file": f"{pname}/{sf.get('name')}".replace("./", ""),
                          "statements": stmts,
                          "missing_lines": sorted(set(missing)),
                          "branches_covered": cb, "branches_total": mb + cb})
    return _doc("jacoco", files)


# —————————————————————————— go coverprofile（go test -coverprofile） ——————————————————————————

_GO_RE = re.compile(r"^(\S+):(\d+)\.\d+,(\d+)\.\d+\s+(\d+)\s+(\d+)$")


def from_go_coverprofile(text: str) -> dict:
    """块级 → 文件级：statements=ΣnumStmt；未覆盖块展开行区间（C3 需要,不含 0 行）。"""
    per: dict[str, dict] = {}
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("mode:") or ln.startswith("{"):
            continue
        m = _GO_RE.match(ln)
        if not m:
            continue
        fname, s_line, e_line, num_stmt, hit = (m.group(1), int(m.group(2)),
                                                int(m.group(3)), int(m.group(4)),
                                                int(m.group(5)))
        agg = per.setdefault(fname, {"stmts": 0, "missing": set()})
        agg["stmts"] += num_stmt
        if hit == 0:
            agg["missing"].update(range(max(1, s_line), e_line + 1))
    files = [{"file": f, "statements": a["stmts"],
              "missing_lines": sorted(a["missing"]),
              "branches_covered": None, "branches_total": None}
             for f, a in sorted(per.items())]
    return _doc("gocover", files)


# —————————————————————————— clover XML（phpunit --coverage-clover） ——————————————————————————

def from_clover_xml(xml_text: str) -> dict:
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return _doc("clover", [])
    files = []
    for f in root.iter("file"):
        metrics = f.find("metrics")
        if metrics is None:
            continue
        missing = [int(ln.get("num")) for ln in f.iter("line")
                   if ln.get("type") == "stmt" and ln.get("count") == "0"]
        files.append({"file": (f.get("name") or "").replace("\\", "/"),
                      "statements": int(metrics.get("statements") or 0),
                      "missing_lines": sorted(set(missing)),
                      "branches_covered": int(metrics.get("coveredconditionals") or 0),
                      "branches_total": int(metrics.get("conditionals") or 0)})
    return _doc("clover", files)
