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


def fingerprint_of(stdout: str, stderr: str) -> str:
    """失败指纹 = pytest 短摘要（-rfE 的 FAILED/ERROR 行）规整后哈希。

    只保留 用例定位 + 错误类型，去掉具体数值消息 —— 断言值变了但错在同一个
    地方仍算同指纹（Agent 换个预期值再撞一次墙，就该熔断）。
    """
    lines = [l.strip() for l in stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
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
    if re.search(r"(ImportError|ModuleNotFoundError|AttributeError|SyntaxError)", text):
        return "case_defect"      # 用例引用了不存在的东西 / 写错了
    if "FAILED" in text or "AssertionError" in text:
        return "product_defect"   # 断言合理但行为不符 → 疑似产品缺陷
    return "env"                  # 其余先按环境问题处理（重装依赖重试一次）


def decide(exit_code: int, stdout: str, stderr: str, fingerprints: list[str],
           fingerprint: str, round_no: int, coverage_line: float | None,
           coverage_target: float | None = None) -> tuple[str, str, str]:
    """R-Loop 判定。返回 (verdict, failure_kind, stop_reason)。

    verdict ∈ pass / repair_env / repair_gen / stop
    coverage_target 用任务行冻结的准出标准（A1），不传时退回全局配置。
    覆盖率解析失败（None）按"未知"处理：不算达标，走补测获取数据（A8）。
    """
    kind = classify_failure(exit_code, stdout, stderr)
    target = coverage_target if coverage_target is not None else config.COVERAGE_TARGET
    coverage_ok = coverage_line is not None and coverage_line >= target

    # 1) 准出标准
    if exit_code == 0 and coverage_ok:
        return "pass", kind, "达标（通过且覆盖率满足准出标准）"

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
