# -*- coding: utf-8 -*-
"""预期依据审计（总包 C7，2026-11-27；旗标 MTA_EXPECT_BASIS_AUDIT 默认关）。

问题实锤（v3 规模批）：需求 #1159 全文 39 字不提 title 也不提 DELETE，go 3/15 格的
"疑似产品缺陷"只靠"空 title 应 400"这类常识预期——无依据预期被判产品缺陷。
本模块在 product_defect 停判时给每条保留失败标注：
  - expected_basis（登记时的依据标签：需求明示/源码推导/常识/模型推断）；
  - 需求可追溯性（expected 文本与需求原文 2-gram 交叠）。
全部失败无依据 → 判决降为「预期存疑待裁决」（kind=expectation_dispute），缺陷清单
分组时不再冒充 product_defect；部分有依据 → 维持 product_defect 但逐条留痕。
"""
from __future__ import annotations

import json
import re

from .case_results import normalize_case_name

UNFOUNDED_BASIS = ("常识", "模型推断", "推断", "约定", "惯常")


def _req_supports(expected: str, req_text: str) -> bool:
    """需求可追溯性（独特 token 匹配，2026-11-27 校准）：expected 的英文词（≥3 字母）、
    数字结果码（4xx/2xx/状态码）、中文 4-gram 任一出现在需求原文=有需求线索。
    不用 2-gram——主题词（"会议室"）与通用词（"返回""应"）会假阳性（单测实锤：
    "删除不存在会议室应失败"被 2-gram"会议"误判可溯）。校准取舍：宁漏判不误判——
    可溯性只是人工裁决的标签，误标"可溯"只会少降级不会冤枉产品。"""
    exp = expected or ""
    req_low = (req_text or "").lower()
    if not exp.strip() or not req_low.strip():
        return False
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", exp):
        if tok.lower() in req_low:
            return True
    for tok in re.findall(r"\d{3}", exp):          # 状态码级数字
        if tok in req_text:
            return True
    for tok in re.findall(r"[\u4e00-\u9fff]{2,}", exp):
        for i in range(len(tok) - 3):
            if tok[i:i + 4] in req_text:
                return True
    return False


def _case_index(conn, task_id: int) -> dict[str, tuple[str, str]]:
    """任务登记用例索引：规范化名 → (expected_basis, expected)。
    2026-11-27 阶段 0 实锤：go/js 渲染层会改名（去 test_ 前缀、加 TestR{轮次}_ 前缀），
    精确名匹配在 go 上一律落空 → 全记"未登记"→ 误判无依据。改用与 origin 判定
    同一套规范化名（case_results.normalize_case_name）。"""
    from .case_results import normalize_case_name
    idx: dict[str, tuple[str, str]] = {}
    for r in conn.execute(
            "SELECT c.name, c.expected_basis, c.expected FROM test_cases c "
            "JOIN test_scripts s ON s.id=c.script_id WHERE s.task_id=?", (task_id,)):
        idx[normalize_case_name(r["name"])] = ((r["expected_basis"] or ""),
                                               (r["expected"] or ""))
    return idx


def is_unfounded(basis: str, expected: str, req_text: str) -> bool:
    """无依据 = 预期在需求里找不到线索，且依据标签属常识/推断一类（或未登记）。"""
    basis = basis or "（未登记）"
    return (not _req_supports(expected or "", req_text)) and (
        any(k in basis for k in UNFOUNDED_BASIS) or basis == "（未登记）")


def audit_retained_failures(conn, task_id: int, stdout: str,
                            failing_nodeids=None) -> dict:
    """逐条保留失败标注依据与可追溯性。返回：
    {"n_failed", "all_unfounded", "basis_summary", "per_case",
     "rows": [{nodeid, name, basis, req_traceable, unfounded}]}"""
    from ..graph.repair import failing_nodeids as _fn
    picker = failing_nodeids or _fn
    nodeids = picker(stdout)
    req_row = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                           (task_id,)).fetchone()
    req_text = (req_row["requirement_text"] if req_row else "") or ""
    idx = _case_index(conn, task_id)
    rows: list[dict] = []
    for nid in nodeids:
        name = (nid or "").split("::")[-1].split("/")[0]
        basis, expected = idx.get(normalize_case_name(name), ("", ""))
        basis = basis or "（未登记）"
        traceable = _req_supports(expected, req_text)
        unfounded = is_unfounded(basis, expected, req_text)
        rows.append({"nodeid": nid, "name": name, "basis": basis,
                     "req_traceable": traceable, "unfounded": unfounded})
    n = len(rows)
    n_unf = sum(1 for r in rows if r["unfounded"])
    basis_summary = "、".join(f"{r['name'][:24]}={r['basis']}"
                              f"{'/需求可溯' if r['req_traceable'] else '/需求不可溯'}"
                              for r in rows[:6])
    return {"n_failed": n, "n_unfounded": n_unf,
            "all_unfounded": bool(n) and n_unf == n,
            "basis_summary": basis_summary,
            "per_case": json.dumps(rows, ensure_ascii=False)[:400],
            "rows": rows}
