"""R-Loop 返工控制器核心：失败指纹、归因分类、熔断与预算判定（纯函数，可单测）。

三个停止条件（对齐借用框架 repairPolicy 的设计）：
- 指纹熔断：同一失败指纹累计出现 >= FINGERPRINT_BREAKER 次 → Agent 原地打转，停
- 轮次预算：补测轮数 >= MAX_REPAIR_ROUNDS → 预算耗尽，停（如实上报）
- 准出标准：全部通过且行覆盖率 >= COVERAGE_TARGET → 达标，停（成功）

诚实纪律：指纹为空（无失败摘要）不参与熔断，避免空指纹误伤；所有停止都带原因。
"""
import hashlib
import re

from .. import config

# 用例自身坏掉的强信号错误类（②定向修复只剪这类；断言失败保留为疑似缺陷证人）
CASE_OWN_DEFECT_ERRORS = {"ImportError", "ModuleNotFoundError", "AttributeError",
                          "SyntaxError", "NameError", "UnboundLocalError",
                          # A1（任务卡-369 20261105）：运行时类型错——369 实锤 4 条
                          # "Assignment to constant variable" 被误诊产品缺陷；
                          # classify 侧带用例侧堆栈共现约束（防产品真抛类型错误伤）
                          "TypeError", "ReferenceError"}


def failing_nodeids(stdout: str) -> list[str]:
    """上一轮失败的 nodeid 列表（与 fingerprint_of 同源解析，pytest -rfE 摘要行）。"""
    out = []
    for l in stdout.splitlines():
        l = l.strip()
        if not l.startswith(("FAILED", "ERROR")):
            continue
        head = l.split(" - ", 1)[0].split(None, 1)
        if len(head) > 1 and "::" in head[1]:
            out.append(head[1].strip())
    return out


def assertion_failure_nodeids(stdout: str) -> list[str]:
    """断言失败用例 nodeid —— §7.2 缺陷证人候选（V8：冻结后继续补测）。

    只认 FAILED 行且错误消息含 assert 断言失败；ImportError 等用例自身坏掉
    的强信号不算证人（那些走定向修复剪枝，不是产品缺陷证据）。
    """
    out = []
    for l in stdout.splitlines():
        l = l.strip()
        if not l.startswith("FAILED") or " - " not in l:
            continue
        parts = l.split(" - ", 1)
        head = parts[0].split(None, 1)
        if len(head) > 1 and "::" in head[1] and "assert" in parts[1].lower():
            out.append(head[1].strip())
    return out


def error_class_of(stdout: str, nodeid: str) -> str:
    """提取某 nodeid 的错误类名；找不到返回空串（未知 → 不剪，保守）。"""
    for l in stdout.splitlines():
        l = l.strip()
        if l.startswith(("FAILED", "ERROR")) and nodeid in l and " - " in l:
            m = re.match(r"([A-Za-z_.]+)", l.split(" - ", 1)[1].strip())
            if m:
                return m.group(1).split(".")[-1]
    return ""


def failure_summary_of(stdout: str, nodeid: str, limit: int = 220) -> str:
    """提取某 nodeid 的失败摘要全文（错误类: 消息，如 `AssertionError: assert 1 == 2`）——
    报告概览"预期 vs 实际"与 AI 缺陷分析的证据源；找不到返回空串。"""
    for l in stdout.splitlines():
        l = l.strip()
        if l.startswith(("FAILED", "ERROR")) and nodeid in l and " - " in l:
            return l.split(" - ", 1)[1].strip()[:limit]
    return ""


def fingerprint_of(stdout: str, stderr: str,
                   exclude_nodeids: tuple[str, ...] = ()) -> str:
    """失败指纹 = pytest 短摘要（-rfE 的 FAILED/ERROR 行）规整后哈希。

    只保留 用例定位 + 错误类型，去掉具体数值消息 —— 断言值变了但错在同一个
    地方仍算同指纹（Agent 换个预期值再撞一次墙，就该熔断）。
    exclude_nodeids：冻结证人（V8）的失败行不进指纹——有意保留的证人每轮
    必失败，算进指纹会在 FINGERPRINT_BREAKER 内误触熔断（打转≠挂账）。
    """
    lines = [l.strip() for l in stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    if exclude_nodeids:
        lines = [l for l in lines
                 if not any(nid in l.split(" - ", 1)[0] for nid in exclude_nodeids)]
        if not lines:
            return ""   # 冻结证人之外无失败行：空指纹，不参与熔断（区别于下面的输出崩溃兜底）
    if not lines:
        # 无短摘要的失败（收集错误/崩溃等）：退化为输出尾部哈希
        tail = "\n".join((stderr or stdout).strip().splitlines()[-5:])
        if not tail:
            return ""
        return hashlib.sha256(tail.encode()).hexdigest()[:16]
    norm = []
    for l in lines:
        # 注意：不能用非贪婪 (\S+?) 无锚点匹配——它会只吃 1 个字符，把所有失败行
        # 塌缩成同一指纹（单测 test_不同用例失败_异指纹 抓到的回归）
        parts = l.split(" - ", 1)
        head = parts[0].split(None, 1)          # ["FAILED", "nodeid"]
        nodeid = head[1].strip() if len(head) > 1 else parts[0]
        err = "Unknown"
        if len(parts) > 1:
            m = re.match(r"([A-Za-z_.]+)", parts[1].strip())
            if m:
                err = m.group(1).split(".")[-1]  # 只留错误类名
        norm.append(f"{nodeid}|{err}")
    return hashlib.sha256("|".join(sorted(norm)).encode()).hexdigest()[:16]


def classify_failure(exit_code: int, stdout: str, stderr: str) -> str:
    """失败归因三类：env / case_defect / product_defect（v1 启发式，可被人工标注纠正）。

    诚实边界：自动归因是启发式，product_defect 仅表示"疑似"，最终定性靠
    feedback_annotations 人工标注 —— 这正是论文误报率实验的数据来源。
    """
    if exit_code == 0:
        return "none"
    text = (stdout or "") + "\n" + (stderr or "")
    if re.search(r"No module named (pytest|coverage)", text) or "pip" in stderr.lower():
        return "env"
    if re.search(r"no tests ran", text, re.I):
        return "case_defect"      # 没有可跑的用例 → 用例侧问题
    if re.search(r"(ImportError|ModuleNotFoundError|AttributeError|SyntaxError"
                 r"|NameError|UnboundLocalError)", text):
        return "case_defect"      # 用例引用了不存在的东西 / 写错了（与 CASE_OWN_DEFECT_ERRORS 同口径）
    # A1（任务卡-369 20261105）：类型/引用错 + 堆栈首帧指向用例侧（vm 沙箱/生成文件）
    # → 用例自身坏。jest 堆栈第二帧总是测试文件调用点，全文共现会把产品抛错误判为
    # 用例侧——按首帧判归属（A2 语义前置）。无 at 帧（pytest 文本形态）不进此分支，
    # 维持原 FAILED 分支（诚实边界：Python 侧 TypeError 归类保持原状）
    if re.search(r"\b(TypeError|ReferenceError)\b", text):
        m = re.search(r"^\s*at\s+(\S+)", text, re.M)
        if m and re.search(r"evalmachine|test_gen_r\d|test_imported|test_gen_fb", m.group(1)):
            return "case_defect"
    # Go panic 帧归属（白包阶段3，S6 堆栈归因分支；A1 语义对齐）：首个非 stdlib .go 帧
    # 指向生成测试文件（TestGen*_test.go）→ case_defect；指向被测源（或全 stdlib 无法
    # 定位）→ product_defect。go_adapter 统一文本保留 panic 段（探针实锤形态：
    # "panic: ..." + "goroutine N [running]:" + "\t<path>.go:NN" 帧）。
    # Python/jest 文本不含 "panic:"（触发词 Go 专属），既有归类零变化。
    if "panic:" in text:
        for fm in re.finditer(r"^\s+(\S+\.go):\d+", text, re.M):
            frame = fm.group(1)
            if "/usr/local/go/src/" in frame:
                continue
            # go 生成物文件名是 CamelCase（TestGenR1_test.go，无下划线）——
            # js 的 test_gen_r\d 形态不匹配，双形态都认（真机实锤 2026-11-07）
            if re.search(r"(?i)test_?gen_?(?:r|fb)\d|test_imported", frame):
                return "case_defect"
            return "product_defect"
        return "product_defect"
    if "FAILED" in text or "AssertionError" in text:
        return "product_defect"   # 断言合理但行为不符 → 疑似产品缺陷
    return "env"                  # 其余先按环境问题处理（重装依赖重试一次）


def decide(exit_code: int, stdout: str, stderr: str, fingerprints: list[str],
           fingerprint: str, round_no: int, coverage_line: float | None,
           coverage_target: float | None = None,
           witness_nodeids: list[str] | None = None) -> tuple[str, str, str]:
    """R-Loop 判定。返回 (verdict, failure_kind, stop_reason)。

    verdict ∈ pass / repair_env / repair_gen / stop
    coverage_target 用任务行冻结的准出标准（A1），不传时退回全局配置。
    覆盖率解析失败（None）按"未知"处理：不算达标，走补测获取数据（A8）。
    witness_nodeids：V8（证人冻结+继续补测）开启时由 evaluate 传入累积证人
    列表；None=开关关，product_defect 首轮即停（既有口径，逐字节不变）。
    """
    kind = classify_failure(exit_code, stdout, stderr)
    target = coverage_target if coverage_target is not None else config.COVERAGE_TARGET
    coverage_ok = coverage_line is not None and coverage_line >= target

    # 1) 准出标准
    if exit_code == 0 and coverage_ok:
        return "pass", kind, "达标（通过且覆盖率满足准出标准）"

    # 1.5) 疑似产品缺陷（vision-and-gaps §7.2）：断言失败=缺陷证人，冻结并如实上报，
    #      不重造用例迁就（防造假）。宁可误报不可造假——误判由人工标注与变异标定纠偏。
    if exit_code != 0 and kind == "product_defect":
        if witness_nodeids is None:
            return "stop", kind, "疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）"
        # V8：证人已冻结挂账，不再首轮停——非证人失败照常分流，只剩证人则按覆盖率决定
        witness_set = set(witness_nodeids)
        others = [n for n in failing_nodeids(stdout) if n not in witness_set]
        if not others:
            if coverage_ok:
                return "stop", kind, (f"补测完成：非证人用例全部通过且覆盖率 {coverage_line:.0%} 达标，"
                                      f"冻结证人 {len(witness_set)} 条如实上报疑似产品缺陷（V8）")
            cov_desc = "未知（解析失败）" if coverage_line is None else f"{coverage_line:.0%}"
            if round_no >= config.MAX_REPAIR_ROUNDS:
                return "stop", kind, (f"冻结证人 {len(witness_set)} 条如实上报疑似产品缺陷，"
                                      f"覆盖率 {cov_desc} 未达标且补测预算耗尽（V8）")
            return "repair_gen", kind, (f"证人 {len(witness_set)} 条已冻结挂账，"
                                        f"覆盖率 {cov_desc} 未达准出标准，继续补测（V8）")
        # 还有非证人失败 → 落到 2)/3)/4) 常规分流（熔断/预算只看排除证人后的指纹）

    # 2) 指纹熔断（空指纹不参与）
    if fingerprint:
        history = fingerprints + [fingerprint]
        if history.count(fingerprint) >= config.FINGERPRINT_BREAKER:
            return "stop", kind, f"同指纹熔断：失败模式重复 {history.count(fingerprint)} 次，Agent 原地打转"

    # 3) 轮次预算
    if round_no >= config.MAX_REPAIR_ROUNDS:
        if exit_code == 0:
            cov_desc = "未知（解析失败）" if coverage_line is None else f"{coverage_line:.0%}"
            return "stop", kind, f"覆盖率{cov_desc}未达准出标准（目标 {target:.0%}）且补测预算耗尽"
        return "stop", kind, f"补测轮次预算耗尽（{config.MAX_REPAIR_ROUNDS} 轮）仍未通过"

    # 4) 分流修复
    if exit_code == 0:
        if coverage_line is None:
            return "repair_gen", kind, "通过但覆盖率未知（TOTAL 行解析失败），补测一轮获取数据"
        return "repair_gen", kind, f"通过但覆盖率 {coverage_line:.0%} 低于目标 {target:.0%}，补测"
    if kind == "env":
        return "repair_env", kind, "环境类失败，修复环境后重跑"
    return "repair_gen", kind, "用例/产品类失败，重新生成用例"
