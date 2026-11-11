"""js_adapter —— jest 输出 → pytest 风格文本适配器（JS/TS 执行链路 Tier2，2026-11-01）。

设计：最小侵入——run 节点在 JS 分支把 `jest --json --coverage` 的 JSON 转成
pytest -q -rfE --cov 的等价文本（passed/failed 计数、FAILED nodeid - ErrorClass 行、
term-missing 覆盖表），下游 evaluate / repair（指纹·归因·剪枝）/ report **零改动**。

错误类映射（让 repair 的 env/case/product 三分类对 JS 直接生效）：
  expect 断言失败            → AssertionError（证人语义：疑似产品缺陷）
  ReferenceError             → NameError（用例自身坏）
  Cannot find module / MODULE_NOT_FOUND → ImportError（用例自身坏/环境）
  SyntaxError / TypeError    → 同名
nodeid 口径：`<文件名>::<用例名>`——提示词要求 jest 用例名写成 test_xxx（无空格），
与 Python nodeid 一致，test_cases.name 反查、剪枝���证人冻结全部复用。

stderr 诊断段（task_366 误诊实锤，2026-11-04）：用例内子进程的报错（Cannot find
module / ECONNREFUSED / ENOENT …）只出现在 jest 进程的 stderr，--json JSON 里只有
expect 详情——成功解析路径此前把 stderr 丢弃，决定性证据在整条归因链蒸发，子进程级
失败被误归因"疑似产品缺陷"。现把诊断行去重计数后附在输出尾部独立段：不带 FAILED/ERROR
前缀（不干扰 per-case 解析与计数），措辞不含 "N failed/errors" 字样（不撞 evaluate
计数正则），同文本逐轮稳定（不抖动 repair 指纹）。
"""
import json
import re
from pathlib import PurePosixPath

_CLASS_MAP = [
    (re.compile(r"Cannot find module|MODULE_NOT_FOUND", re.I), "ImportError"),
    (re.compile(r"\bReferenceError\b"), "NameError"),
    (re.compile(r"\bSyntaxError\b"), "SyntaxError"),
    (re.compile(r"\bTypeError\b"), "TypeError"),
    (re.compile(r"\bRangeError\b"), "ValueError"),
    (re.compile(r"expect\(|toBe|toEqual|toThrow|AssertionError|Expected:", re.I), "AssertionError"),
]


def _error_class(msg: str) -> str:
    head = (msg or "")[:600]
    for rx, cls in _CLASS_MAP:
        if rx.search(head):
            return cls
    return "Error"


# 子进程级诊断行：行首即错误标识（jest stderr 里子进程报错的首行形态；
# Require stack: / at ... 堆栈行不匹配=自然过滤）
_STDERR_DIAG_RE = re.compile(
    r"^\s*((?:Error|Cannot find module|MODULE_NOT_FOUND|ENOENT|ECONNREFUSED"
    r"|EACCES|EADDRINUSE)[^\n]*)$", re.M)
_DIAG_HEADER = "---------------------- stderr diagnostics (jest) ----------------------"


def stderr_diagnostics(stderr: str) -> str:
    """从 jest stderr 提取子进程级诊断行（去重计数）；无匹配返回空串（零噪声）。"""
    seen: dict[str, int] = {}
    for m in _STDERR_DIAG_RE.finditer(stderr or ""):
        line = m.group(1).strip()
        seen[line] = seen.get(line, 0) + 1
    if not seen:
        return ""
    rows = [line if cnt == 1 else f"{line} × {cnt}" for line, cnt in seen.items()]
    return _DIAG_HEADER + "\n" + "\n".join(rows)


def _nodeid(file_path: str, full_name: str, title: str) -> str:
    """nodeid = ��对文件名::用例名。优先用 it/test 的 title（生成器写成 test_xxx）；
    空格→下划线保证 `FAILED (\\S+)` 可解析。"""
    fname = PurePosixPath(file_path.replace("\\", "/")).name
    name = (title or full_name or "unnamed").strip()
    name = re.sub(r"\s+", "_", name)
    return f"{fname}::{name}"


LAST_UNIFIED_COVERAGE: dict | None = None   # C2 统一层通道（最近一次 jest coverageMap 的统一 schema）


def _coverage_rows(coverage_map: dict) -> tuple[list[str], int, int]:
    """coverageMap（istanbul 形态）→ term-missing 行；返回 (rows, total_stmts, total_miss)。

    任务卡-F 阶段 1 #7（2026-11-10）C2 统一层接线：同 coverageMap 经
    coverage_adapter.from_istanbul 出统一 schema（供 C3 回灌/看板跨语言通道）；
    本函数保留 term-missing 文本渲染（字节兼容），双解析互证。"""
    try:
        from .coverage_adapter import from_istanbul
        unified = from_istanbul(coverage_map or {})
        # 统一层数据供下游（per_file 形态挂在模块级供调用方取用——不改变本函数输出）
        global LAST_UNIFIED_COVERAGE
        LAST_UNIFIED_COVERAGE = unified
    except Exception:                 # noqa: BLE001 —— 统一层失败不影响文本渲染主链路
        import sys as _sys
        LAST_UNIFIED_COVERAGE = None
    rows: list[str] = []
    tot_s = tot_m = 0
    for path, fc in (coverage_map or {}).items():
        smap = fc.get("statementMap") or {}
        hits = fc.get("s") or {}
        n = len(smap)
        if not n:
            continue
        miss_lines = sorted({smap[k].get("start", {}).get("line", 0)
                             for k, cnt in hits.items() if not cnt and k in smap})
        miss = sum(1 for k, cnt in hits.items() if not cnt)
        cover = round((n - miss) / n * 100)
        rel = path.replace("\\", "/")
        rel = rel.split("/work/", 1)[-1] if "/work/" in rel else PurePosixPath(rel).name
        missing = ", ".join(str(l) for l in miss_lines if l) if miss_lines else ""
        rows.append(f"{rel:<28}{n:>8}{miss:>7}{cover:>6}%   {missing}")
        tot_s += n
        tot_m += miss
    return rows, tot_s, tot_m


def _extract_trailing_json(text: str):
    """从混杂输出中提取最长可解析 JSON（晚间回归实锤：贪婪正则 `{.*}` 会被前缀里的
    `{}` 带偏，整段解析失败→计数全丢）。逐个 `{` 位置 raw_decode，取 end 最大（最长）
    的成功解析——容忍 JSON 前后的任意噪声。"""
    dec = json.JSONDecoder()
    best_obj, best_end = None, 0
    if len(text) > 2_000_000:            # 防病态输入 O(n²) 爆炸
        text = text[-2_000_000:]
    for i, ch in enumerate(text):
        if ch != "{" or i < best_end:    # 已找到更长解析后，跳过其内部的 `{`
            continue
        try:
            obj, end = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if end > best_end:
            best_obj, best_end = obj, end
    return best_obj


def jest_json_to_pytest_text(stdout: str, stderr: str = "") -> str:
    """主入口：jest --json 输出（stdout 为 JSON；可能混杂非 JSON 前缀）→ pytest 风格文本。
    解析失败（无测试/配置炸/非 JSON）原样返回 stdout+stderr 让 evaluate 走 env 归因。"""
    data = _extract_trailing_json(stdout or "")
    if not isinstance(data, dict) or "testResults" not in data:
        return (stdout or "") + ("\n" + stderr if stderr else "")
    passed = int(data.get("numPassedTests") or 0)
    failed = int(data.get("numFailedTests") or 0)
    pending = int(data.get("numPendingTests") or 0)
    errors = int(data.get("numRuntimeErrorTestSuites") or 0)
    lines: list[str] = []
    for suite in data.get("testResults") or []:
        fpath = suite.get("name") or suite.get("testFilePath") or ""
        # 套件级崩溃（import 炸/语法错）：无用例行，按 ERROR 文件级报
        if suite.get("status") == "failed" and not suite.get("assertionResults"):
            msg = (suite.get("message") or "")[:300].replace("\n", " ")
            lines.append(f"ERROR {PurePosixPath(fpath.replace(chr(92), '/')).name} - "
                         f"{_error_class(msg)}: {msg}")
            continue
        for a in suite.get("assertionResults") or []:
            if a.get("status") != "failed":
                continue
            msgs = " ".join(a.get("failureMessages") or [])
            cls = _error_class(msgs)
            short = re.sub(r"\s+", " ", msgs)[:240]
            lines.append(f"FAILED {_nodeid(fpath, a.get('fullName', ''), a.get('title', ''))} "
                         f"- {cls}: {short}")
    cov_rows, tot_s, tot_m = _coverage_rows(data.get("coverageMap") or {})
    out = []
    if lines:
        out.append("=========================== short test summary info ===========================")
        out.extend(lines)
    if cov_rows:
        out.append("")
        out.append("---------- coverage: platform jest ----------")
        out.append(f"{'Name':<28}{'Stmts':>8}{'Miss':>7}{'Cover':>7}   Missing")
        out.extend(cov_rows)
        pct = round((tot_s - tot_m) / tot_s * 100) if tot_s else 0
        out.append(f"{'TOTAL':<28}{tot_s:>8}{tot_m:>7}{pct:>6}%")
    elif data.get("coverageMap") is not None and not cov_rows and lines:
        # D（任务卡-369 20261105）覆盖率断点诊断：jest --coverage 正常产出但
        # coverageMap files=0 —— 用例以 readFileSync+vm.runInContext 加载被测代码
        # （绕过 require 插桩管线，istanbul 零插桩；require 型用例覆盖率正常）。
        # 显式说明替代裸 N/A：报告/人工不再猜测覆盖率为什么没有
        out.append("")
        out.append("---------- coverage: unavailable ----------")
        out.append("coverageMap 为空：用例未通过 require 加载被测代码"
                   "（vm/readFileSync 模式无法插桩）——覆盖率不可用，非解析断点")
    # 套件崩溃计入 failed（853刷题系统实锤：无工程文件的散装 JS 套件 require 崩溃，
    # 0 passed/0 failed 只有 errors 行 → evaluate 判"没解析到"走 repair_env 循环熔断；
    # 计入 failed 后归因走 case_defect——生成用例与项目模块系统不匹配是用例侧问题）
    if errors and failed == 0:
        failed = errors
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    parts.append(f"{passed} passed")
    if pending:
        parts.append(f"{pending} skipped")
    if errors:
        parts.append(f"{errors} errors")
    out.append(f"===================== {', '.join(parts)} in jest =====================")
    diag = stderr_diagnostics(stderr)
    if diag:
        out.extend(["", diag])
    return "\n".join(out)
