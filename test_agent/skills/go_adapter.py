"""go_adapter：go test -json + coverprofile → pytest 风格统一文本（白包阶段3，docs/多语言工具链接入任务包-20261106.md）。

S5 扩展枢纽：adapter 输出统一文本，下游 evaluate/repair/report 零改动（任务卡纪律）。
- go test -json：行分隔 JSON 事件流（容器探针实锤 2026-11-07：Action ∈ start/run/output/
  pause/cont/pass/fail/skip；Test 级终态带 Test+Elapsed；编译失败=包级 fail+[build failed]，
  构建错误在 `# pkg [pkg.test]` 前缀的 output 事件里）；非 JSON 行跳过（诚实降级）。
- coverprofile：`mode: set` 头 + `pkg/file.go:l.c,l.c numstmt hit` 行（探针实锤；
  hit>0=覆盖）——文件级缺口表只取 miss>0 的文件（B2 口径对齐 jacoco 覆盖表：
  无缺口证据=覆盖），TOTAL 汇总行与 evaluate 的 `TOTAL.*?(\\d+)%` 正则同形。
- panic 块保留进统一文本（归因三闸消费堆栈帧——classify_failure 的 go panic 分支，
  帧归属：首个非 /usr/local/go/src 帧，指向 TestGen*_test.go=用例侧，指向被测源=产品侧）。
"""
import json
import re


def _parse_events(json_text: str) -> list[dict]:
    """行分隔 JSON 事件流解析；非 JSON 行跳过（go test 偶发 stderr 噪声）。"""
    events = []
    for ln in (json_text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _err_message(outputs: list[str]) -> str:
    """失败消息合成：优先 file.go:NN 前缀的断言行，退化首个非 ===/--- 行。"""
    detail = [ln for ln in outputs if not ln.startswith(("=== ", "--- ", "### "))]
    for ln in detail:
        if re.search(r"\S+\.go:\d+", ln):
            return ln.strip()[:300]
    return (detail[0].strip()[:300] if detail else "")


def _build_err_lines(json_text: str, stderr_text: str = "") -> list[str]:
    """编译错误细节行收集（658/659 实锤）：build_run_cmd_go 带 2>&1——编译错误
    （# pkg 头/vet:/undefined/.go:行）混在 stdout 的非 JSON 散行里被 _parse_events
    跳过，stderr 键反而常为空——两处都扫，保归因链（task_366 stderr 丢弃同族）。"""
    raw = list((json_text or "").splitlines()) + list((stderr_text or "").splitlines())
    return [ln.strip() for ln in raw
            if ln.strip() and not ln.strip().startswith("{")
            and (".go:" in ln or "undefined:" in ln or "declared" in ln or "Error" in ln)]


def _fill_build_err(build_err: dict, build_failed: set, err_lines: list[str]) -> None:
    """把编译错误细节行填给 build_failed 包（首个错误行，防 # pkg 头行覆盖细节）。"""
    if not err_lines:
        return
    for pkg in build_failed:
        if pkg not in build_err:
            build_err[pkg] = err_lines[0][:300]


def _synthesize_no_event_error(json_text: str, stderr_text: str = "") -> dict | None:
    """零 JSON 事件的失败合成（2026-11-08 任务 678/679 实锤）：工具链版本不匹配时
    `go test` 只吐一行散行（"go: go.mod requires go >= 1.26.0 …"），既非 JSON 事件
    也没有 `[build failed]`——原实现产出 0 结果行、错误详情完全蒸发（evaluate 只看
    到 0 通过 0 失败 → env → repair_env 空转，归因链断，与 task_366 stderr 丢弃同族）。
    此处把散行合成为一条包级 ERROR，让台账/报告直接显示真实原因。"""
    raw = list((json_text or "").splitlines()) + list((stderr_text or "").splitlines())
    lines = [ln.strip() for ln in raw if ln.strip() and not ln.strip().startswith("{")]
    if not lines:
        return None
    detail = " ".join(lines)[:300]
    return {"nodeid": "go::[no test events]", "name": "[no test events]",
            "status": "error", "duration_ms": None,
            "message": f"go test 未产出任何事件（疑似环境/工具链问题）：{detail}"}


def go_test_json_to_rows(json_text: str, stderr_text: str = "") -> list[dict]:
    """go test -json → 逐用例 rows（store_case_results 同契约：nodeid/name/status/
    duration_ms/message）。编译失败无 Test 事件 → 包级 ERROR 行（nodeid=pkg::[build failed]）。"""
    events = _parse_events(json_text)
    outputs: dict[tuple[str, str], list[str]] = {}
    terminal: dict[tuple[str, str], dict] = {}
    build_failed: set[str] = set()
    build_err: dict[str, str] = {}
    for ev in events:
        pkg, test = ev.get("Package", ""), ev.get("Test") or ""
        if ev.get("Action") == "output":
            outputs.setdefault((pkg, test), []).append(ev.get("Output") or "")
            out = (ev.get("Output") or "").strip()
            if "[build failed]" in out and not test:
                build_failed.add(pkg)
            # 编译错误细节行优先于 `# pkg` 头行（消息合成取更有信息量的那行）
            elif not test:
                if out.startswith("# "):
                    build_err.setdefault(pkg, out[:300])
                elif ("Error" in out or "undefined" in out or ".go:" in out):
                    if pkg in build_err and build_err[pkg].startswith("# "):
                        build_err[pkg] = out[:300]
                    else:
                        build_err.setdefault(pkg, out[:300])
        elif ev.get("Action") in ("pass", "fail", "skip") and test:
            terminal[(pkg, test)] = ev
    rows = []
    for (pkg, test), ev in sorted(terminal.items()):
        action = ev["Action"]
        status = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
        outs = outputs.get((pkg, test), [])
        rows.append({"nodeid": f"{pkg}::{test}", "name": test, "status": status,
                     "duration_ms": int(round(float(ev.get("Elapsed") or 0) * 1000)),
                     "message": _err_message(outs)})
    if build_failed:
        _fill_build_err(build_err, build_failed, _build_err_lines(json_text, stderr_text))
    for pkg in sorted(build_failed):
        rows.append({"nodeid": f"{pkg}::[build failed]", "name": "[build failed]",
                     "status": "error", "duration_ms": None,
                     "message": build_err.get(pkg, "build failed")})
    if not rows:
        synth = _synthesize_no_event_error(json_text, stderr_text)
        if synth:
            rows.append(synth)
    return rows


def coverprofile_to_rows(cover_text: str) -> tuple[list[dict], int]:
    """coverprofile → (有缺口的文件级行, 总覆盖百分比)。B2 口径：全覆盖文件不进行列表。"""
    per: dict[str, dict[str, int]] = {}
    missing: dict[str, list[int]] = {}
    for ln in (cover_text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("mode:") or ln.startswith("{"):
            continue
        m = re.match(r"^(\S+):(\d+)\.\d+,\d+\.\d+\s+(\d+)\s+(\d+)$", ln)
        if not m:
            continue
        fname, _, stmts, hit = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        agg = per.setdefault(fname, {"stmts": 0, "covered": 0})
        agg["stmts"] += stmts
        agg["covered"] += stmts if hit > 0 else 0
        if hit == 0:
            missing.setdefault(fname, []).append(int(m.group(2)))
    rows, tot_s, tot_c = [], 0, 0
    for fname in sorted(per):
        agg = per[fname]
        tot_s += agg["stmts"]
        tot_c += agg["covered"]
        miss = agg["stmts"] - agg["covered"]
        if miss > 0:
            rows.append({"file": fname, "miss": miss, "cover": agg["covered"],
                         "pct": round(100 * agg["covered"] / agg["stmts"]) if agg["stmts"] else 0,
                         "missing": sorted(set(missing.get(fname, [])))})
    total_pct = round(100 * tot_c / tot_s) if tot_s else 0
    return rows, total_pct


def go_test_json_to_pytest_text(json_text: str, cover_text: str | None = None,
                                stderr_text: str = "") -> str:
    """go test -json → pytest 风格统一文本（计数/FAILED/ERROR 行/panic 帧段/覆盖表）。

    evaluate 正则契约对齐：`(\\d+) passed` / `(\\d+) failed` / `TOTAL.*?\\s(\\d+)%`。"""
    events = _parse_events(json_text)
    outputs: dict[tuple[str, str], list[str]] = {}
    terminal: dict[tuple[str, str], dict] = {}
    build_failed: set[str] = set()
    build_err: dict[str, str] = {}
    panic_lines: list[str] = []
    in_panic = False
    for ev in events:
        pkg, test = ev.get("Package", ""), ev.get("Test") or ""
        out = ev.get("Output") or ""
        if ev.get("Action") == "output":
            outputs.setdefault((pkg, test), []).append(out)
            if "[build failed]" in out and not test:
                build_failed.add(pkg)
            # 编译错误细节行优先于 `# pkg` 头行（与 rows 函数同口径）
            o = out.strip()
            if not test:
                if o.startswith("# "):
                    build_err.setdefault(pkg, o[:300])
                elif ("Error" in o or "undefined" in o or ".go:" in o):
                    if pkg in build_err and build_err[pkg].startswith("# "):
                        build_err[pkg] = o[:300]
                    else:
                        build_err.setdefault(pkg, o[:300])
            if re.search(r"\bpanic:", out):
                in_panic = True
                panic_lines.append(out.rstrip("\n"))
            elif in_panic and out.startswith("--- FAIL"):
                in_panic = False
            elif in_panic and re.search(r"goroutine \d+ \[running\]|\.go:\d+", out):
                panic_lines.append(out.rstrip("\n"))
        elif ev.get("Action") in ("pass", "fail", "skip") and test:
            terminal[(pkg, test)] = ev
            in_panic = False
    passed = sum(1 for ev in terminal.values() if ev["Action"] == "pass")
    skipped = sum(1 for ev in terminal.values() if ev["Action"] == "skip")
    failed = sum(1 for ev in terminal.values() if ev["Action"] == "fail")
    lines: list[str] = ["=========================== short test summary info ==========================="]
    body: list[str] = []
    for (pkg, test), ev in sorted(terminal.items()):
        outs = outputs.get((pkg, test), [])
        msg = _err_message(outs)
        if ev["Action"] == "fail":
            body.append(f"FAILED {pkg}::{test} - {msg}")
        elif ev["Action"] == "skip":
            body.append(f"SKIP {pkg}::{test} - {msg}")
    if build_failed:
        _fill_build_err(build_err, build_failed, _build_err_lines(json_text, stderr_text))
    for pkg in sorted(build_failed):
        body.append(f"ERROR {pkg}::[build failed] - {build_err.get(pkg, 'build failed')}")
        failed += 1
    if not terminal and not build_failed:
        # 零事件但有散行（工具链版本不匹配等）→ 合成包级错误，别让详情蒸发
        synth = _synthesize_no_event_error(json_text, stderr_text)
        if synth:
            body.append(f"ERROR {synth['nodeid']} - {synth['message']}")
            failed += 1
    lines.extend(body)
    lines.append(f"=========================== {passed} passed, {failed} failed, "
                 f"{skipped} skipped ===========================")
    if panic_lines:
        lines.append("---------- go panic (top frames) ----------")
        lines.extend(panic_lines[:20])
    if cover_text:
        rows, total = coverprofile_to_rows(cover_text)
        lines.append("---------- coverage: platform coverprofile ----------")
        lines.append(f"{'Name':<44}{'Miss':>6}{'Cover':>8}   Missing")
        for r in rows:
            lines.append(f"{r['file']:<44}{r['miss']:>6}{r['pct']:>7}%   "
                         f"{','.join(map(str, r['missing']))}")
        # TOTAL 无条件输出（全覆盖=无缺口行时也要如实呈现 100%，否则 evaluate 拿到
        # coverage=null 被误读为"未采集"——真机实锤：LLM 用例打了 100 分正确路径，
        # 埋缺陷分支的 return "A" 被覆盖 → 无缺口行）
        lines.append(f"{'TOTAL':<44}{'':>6}{total:>7}%")
    return "\n".join(lines) + "\n"
