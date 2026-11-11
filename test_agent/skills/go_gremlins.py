# -*- coding: utf-8 -*-
"""Go 变异标定管线（任务卡-F2 阶段 1 #9：W5 mutmut→gremlins，2026-11-11）。

gremlins unleash（容器内）→ 文本摘要解析 → {killed, lived, efficacy, mutator_coverage}。
真机锚点（go-shop 手写套件实测）：Killed 3/Lived 2/efficacy 60.00%/mutator 100%——
LIVED 的 CONDITIONALS_BOUNDARY 恰暴露套件未测 90 边界邻居（有信息量的存活）。
单测锁解析口径；容器执行由调用方传入（脚本 scripts/go_gremlins.py 一键）。
"""
import re

_SUM_RE = re.compile(
    r"Killed:\s*(\d+),\s*Lived:\s*(\d+),\s*Not covered:\s*(\d+)")
_EFF_RE = re.compile(r"Test efficacy:\s*([\d.]+)%")
_COV_RE = re.compile(r"Mutator coverage:\s*([\d.]+)%")
_LIVED_RE = re.compile(r"\bLIVED\s+(\w+)\s+at\s+([^\s:]+):(\d+)")


def parse_gremlins(text: str) -> dict:
    """gremlins unleash 文本摘要 → 结构化。摘要行缺失=诚实空（产物未生成）。"""
    out: dict = {"killed": None, "lived": None, "not_covered": None,
                 "efficacy": None, "mutator_coverage": None,
                 "lived_mutations": []}
    m = _SUM_RE.search(text or "")
    if m:
        out["killed"], out["lived"], out["not_covered"] = map(int, m.groups())
    m = _EFF_RE.search(text or "")
    if m:
        out["efficacy"] = float(m.group(1))
    m = _COV_RE.search(text or "")
    if m:
        out["mutator_coverage"] = float(m.group(1))
    out["lived_mutations"] = [
        {"kind": m2.group(1), "file": m2.group(2), "line": int(m2.group(3))}
        for m2 in _LIVED_RE.finditer(text or "")]
    return out


def kill_rate(parsed: dict) -> float | None:
    """kill_rate = killed / (killed+lived)；无变异体时 None（诚实）。"""
    k, l = parsed.get("killed"), parsed.get("lived")
    if k is None or l is None or k + l == 0:
        return None
    return round(k / (k + l), 3)
