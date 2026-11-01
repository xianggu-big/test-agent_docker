"""AI 代审（代理人工确认/审查）——审查员角色的 Agent 化，不是绕过阀门。

设计权威：docs/AI代审设计-20261031.md。核心纪律：
- **角色分离红线**（与 V9 同源）：代审提示词 ≠ 生成提示词（SYSTEM_PROMPT/REVIEW_SYSTEM 均不同角色）；
- **决策必须结构化+可解释**：reasoning 必须引用证据（条件编号/断言类型/覆盖缺口），
  静态闸校验理由非空且引用的 condition_id 真实存在（防 vibes 审查）；
- **证人保护**：疑似产品缺陷用例（历史断言失败）AI 无权删除/重写（宁可误报不可造假）；
- **fail-closed**：代审 LLM 失败/输出不可解析 → 返回 None，由调用方停在等待点交人工。
"""
import json
import logging
import re

from .. import config
from .llm_usage import log_llm_call
from .prompt_library import D1_SYSTEM, REVIEWER_SYSTEM  # 角色分离红线：提示词知识库集中地

log = logging.getLogger(__name__)


def reviewer_llm():
    """代审专用 LLM（同模型角色分离；跨模型列展望）。延迟导入，无 Key 时由调用方兜底。"""
    import os
    from langchain_openai import ChatOpenAI
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，AI 代审不可用（fail-closed 交人工）")
    return ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                      base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                      timeout=90, max_retries=2)


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ==================== D1 准入（决策点 1：确认门 confirmed 0→1） ====================

def build_d1_prompt(requirement: str, repo_url: str) -> str:
    return f"""## 任务上下文
仓库：{repo_url or "（未提供）"}

## 自然语言需求
{requirement[:3000]}

## 准入审查任务
像资深测试负责人写准入评审意见一样回答：
1. 该需求是否可测（有可验证的行为陈述）？
2. 值得投入容器执行资源吗（风险/收益）？
3. 主要风险点是什么（后续用例审查要盯的地方）？

## 输出格式
{{"decision": "approve 或 reject",
  "reasoning": "为什么值得测/不值得测（必须具体，不许空话）",
  "evidence": {{"risk_points": ["风险点1", "风险点2"]}}}}"""


def parse_d1(text: str) -> dict | None:
    """D1 输出解析：decision∈{approve,reject} 且 reasoning 非空；否则 None（fail-closed）。"""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    if data.get("decision") not in ("approve", "reject"):
        return None
    if not str(data.get("reasoning") or "").strip():
        return None
    return {"decision": data["decision"], "reasoning": str(data["reasoning"]).strip()[:600],
            "evidence": data.get("evidence") if isinstance(data.get("evidence"), dict) else {}}


def run_d1_review(llm, conn, task_id: int, requirement: str, repo_url: str) -> dict | None:
    """D1 准入代审：写审查意见书并落库 review_decisions（决策点 1）。
    返回解析结果；不可解析返回 None（调用方 fail-closed 停等人工，绝不自动放行）。"""
    resp = llm.invoke([("system", D1_SYSTEM), ("user", build_d1_prompt(requirement, repo_url))])
    log_llm_call(task_id, "ai_review_d1", resp)
    parsed = parse_d1(resp.content)
    if parsed is None:
        return None
    conn.execute(
        """INSERT INTO review_decisions(task_id, decision, target, reasoning, evidence,
                                        reviewer_role, round) VALUES (?,?,?,?,?,'ai_proxy',1)""",
        (task_id, parsed["decision"], "task", parsed["reasoning"],
         json.dumps(parsed["evidence"], ensure_ascii=False)))
    conn.commit()
    return parsed


# ==================== D2 用例集审查（核心：两阶段"待审查"停等点） ====================

def build_d2_prompt(requirement: str, conditions: list[dict], cases: list[dict]) -> str:
    cond_rows = "\n".join(f"- 条件{id}：{c['condition']}（优先级 {c.get('priority') or '中'}）"
                          for id, c in conditions) or "（无测试条件）"
    case_rows = "\n".join(
        f"- {c['name']}｜方法 {c.get('method') or '未标注'}｜"
        f"预期 {(c.get('expected') or '未标注')[:70]}｜依据 {(c.get('expected_basis') or '未标注')[:40]}\n"
        f"  代码：{(c.get('content') or c.get('code') or '')[:300]}"
        for c in cases) or "（无用例）"
    return f"""## 用户需求
{requirement[:2500]}

## 测试条件（RTM，审查引用证据时的编号依据）
{cond_rows}

## 待审查的测试用例
{case_rows}

## 审查任务（像人写审查意见：每个决策都要有理由+证据）
1. approve：通过的用例（target=用例名，reasoning 引用其覆盖的条件编号）
2. reject：建议删除的用例（重复覆盖同一行为 / 弱断言无证伪能力 / 有迁就实现反推的嫌疑）
3. regenerate：要求再生成的用例（target=用例名，feedback 字段写清给生成方的增补要求）
4. insert：缺失的行为（target=缺失行为描述，feedback 字段写清要补什么用例）

红线：不确定的用例宁可 approve 并在 reasoning 里注明疑虑，不可无证据删除。

## 输出格式
{{"decisions": [{{"decision": "approve/reject/regenerate/insert",
   "target": "用例名 或 用例集 或 缺失行为描述",
   "reasoning": "引用条件编号/断言类型/覆盖缺口的审查理由",
   "feedback": "（仅 regenerate/insert 需要）给生成方的增补要求文本",
   "evidence": {{"condition_ids": [1, 2]}}}}]}}"""


def parse_review_decisions(text: str) -> list[dict] | None:
    """D2 输出解析：decisions 数组，元素须含合法 decision + 非空 target/reasoning。
    一条有效决策都没有 = 没审 → None（fail-closed）。"""
    data = _extract_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return None
    valid = []
    for d in data["decisions"]:
        if not isinstance(d, dict):
            continue
        if d.get("decision") not in ("approve", "reject", "regenerate", "insert"):
            continue
        target = str(d.get("target") or "").strip()
        reasoning = str(d.get("reasoning") or "").strip()
        if not target or not reasoning:
            continue
        valid.append({"decision": d["decision"], "target": target,
                      "reasoning": reasoning[:600],
                      "feedback": str(d.get("feedback") or "").strip()[:800],
                      "evidence": d.get("evidence") if isinstance(d.get("evidence"), dict) else {}})
    return valid or None


def witness_protected_names(log_lines: list[str]) -> set[str]:
    """证人保护名单（纯函数，AI代审红线）：历史执行日志中 AssertionError 失败的生成用例
    = 疑似 product_defect 证人（§7.2）。AI 代审无权删除或重写它们——宁可误报不可造假。
    log_lines: agent_messages 内容行（pytest -rfE 摘要行「FAILED nodeid - AssertionError: …」）。"""
    out: set[str] = set()
    for line in log_lines:
        m = re.search(r"FAILED (\S+)", line or "")
        if not m or "AssertionError" not in (line or ""):
            continue
        nodeid = m.group(1)
        if nodeid.split("::")[0].startswith(("test_gen_r", "test_gen_fb", "test_imported")):
            out.add(nodeid.split("::")[-1].split("[")[0])
    return out


def validate_decisions(decisions: list[dict], condition_ids: set[int],
                       case_names: set[str], protected_names: set[str]) -> tuple[list[dict], list[str]]:
    """决策静态闸（防 vibes 审查，纯函数）：
    - reasoning 非空（解析层已保，双保险）；
    - evidence.condition_ids 必须真实存在——引用不存在的条件号=幻觉审查，整条剔除；
    - reject/regenerate 的 target 必须是真实用例名；approve 的 target 是用例名或「用例集」；
    - 证人保护：protected 名单上的用例 AI 无权 reject/regenerate（决策剔除并留理由）。
    返回 (合法决策, 剔除理由列表)。"""
    kept, dropped = [], []
    for d in decisions:
        name = f"{d['decision']}:{d['target'][:40]}"
        if not d.get("reasoning", "").strip():
            dropped.append(f"{name}: 理由为空（vibes 审查）")
            continue
        ev = d.get("evidence") or {}
        cids = ev.get("condition_ids")
        if isinstance(cids, list) and cids:
            bad = [c for c in cids if not isinstance(c, int) or c not in condition_ids]
            if bad:
                dropped.append(f"{name}: 引用不存在的条件号 {bad}（幻觉证据）")
                continue
        if d["decision"] in ("reject", "regenerate"):
            if d["target"] in protected_names:
                dropped.append(f"{name}: 证人保护——疑似产品缺陷用例 AI 无权删除/重写")
                continue
            if d["target"] not in case_names:
                dropped.append(f"{name}: 目标用例不存在")
                continue
        if d["decision"] == "approve" and d["target"] not in case_names and d["target"] != "用例集":
            dropped.append(f"{name}: 目标用例不存在")
            continue
        kept.append(d)
    return kept, dropped


def run_review_once(llm, conn, task_id: int, round_no: int = 1) -> dict | None:
    """D2 用例集代审一轮：读需求+条件+用例集 → 结构化决策 → 静态闸校验 → 落库 → 执行删除。
    返回 {approved, deleted, regenerate, inserted, feedback_texts, dropped}；
    LLM 不可解析返回 None（fail-closed 由调用方停等人工）。"""
    requirement = conn.execute("SELECT requirement_text FROM test_tasks WHERE id=?",
                               (task_id,)).fetchone()["requirement_text"] or ""
    conditions = [(r["id"], dict(r)) for r in conn.execute(
        "SELECT id, condition, priority FROM test_conditions WHERE task_id=? ORDER BY id",
        (task_id,))]
    cases = [dict(r) for r in conn.execute(
        """SELECT c.name, c.method, c.expected, c.expected_basis, c.content, c.enabled
           FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.enabled=1 ORDER BY c.id""", (task_id,))]
    if not cases:
        return {"approved": 0, "deleted": 0, "regenerate": 0, "inserted": 0,
                "feedback_texts": [], "dropped": [], "note": "无用例可审（阶段一零产出）"}

    resp = llm.invoke([("system", REVIEWER_SYSTEM),
                       ("user", build_d2_prompt(requirement, conditions, cases))])
    log_llm_call(task_id, "ai_review_d2", resp)
    decisions = parse_review_decisions(resp.content)
    if decisions is None:
        return None

    # 证人保护名单：历史执行日志中的断言失败生成用例（重跑任务 / R-Loop 之前的证据）
    log_lines = [r["content"] for r in conn.execute(
        """SELECT m.content FROM agent_messages m JOIN agent_sessions s ON s.id = m.session_id
           WHERE s.task_id=? ORDER BY m.id""", (task_id,))]
    protected = witness_protected_names(log_lines)
    kept, dropped = validate_decisions(decisions, {c[0] for c in conditions},
                                       {c["name"] for c in cases}, protected)

    approved = deleted = regenerate = inserted = 0
    feedback_texts: list[str] = []
    for d in kept:
        fb = d.get("feedback") or d["reasoning"]
        if d["decision"] == "reject":
            # 删除=停用（保留审计记录，布考场重建文件时摘除）；证人保护已在前置校验拦截
            conn.execute(
                """UPDATE test_cases SET enabled=0 WHERE enabled=1 AND name=? AND script_id IN
                   (SELECT id FROM test_scripts WHERE task_id=?)""", (d["target"], task_id))
            deleted += 1
        elif d["decision"] == "approve":
            approved += 1
        elif d["decision"] == "regenerate":
            regenerate += 1
            feedback_texts.append(f"【重写 {d['target']}】{fb}")
        elif d["decision"] == "insert":
            inserted += 1
            feedback_texts.append(f"【增补】{d['target']}：{fb}")
        conn.execute(
            """INSERT INTO review_decisions(task_id, decision, target, reasoning, evidence,
                                            reviewer_role, round) VALUES (?,?,?,?,?,'ai_proxy',?)""",
            (task_id, d["decision"], d["target"][:300], d["reasoning"],
             json.dumps(d.get("evidence") or {}, ensure_ascii=False), round_no))
    conn.commit()
    return {"approved": approved, "deleted": deleted, "regenerate": regenerate,
            "inserted": inserted, "feedback_texts": feedback_texts, "dropped": dropped}


# ==================== hybrid 抽检（20% 抽查 + 推翻落库 = 对齐信号） ====================

def spotcheck_sample(task_id: int, decision_ids: list[int], ratio: float = 0.2) -> set[int]:
    """hybrid 抽检清单（确定性）：固定种子按任务号抽 20%（至少 1 条）。
    种子=任务号 → 同一决策集多次取清单稳定（3s 轮询重渲染不跳变）。"""
    import random
    if not decision_ids:
        return set()
    rng = random.Random(f"spotcheck-{task_id}")
    n = max(1, round(len(decision_ids) * ratio))
    return set(rng.sample(decision_ids, min(n, len(decision_ids))))


def record_spotcheck(conn, decision_id: int, verdict: str, note: str, user_id: int) -> dict:
    """抽检结论落库（reviewer_role='human'）：
    - uphold：同意 AI 决策——追加一条同决策的人工记录（对齐信号）；
    - override：推翻——追加反向决策的人工记录；reject 类推翻同时恢复用例启用
      （人工意志即时生效，AI 的删除不覆盖人工结论）。返回 {decision, restored}。
    幂等防重：同一决策同一结论已存在时直接返回既有记录不重复落行
    （E2E 实锤：前端无原地反馈时用户会重复点击，台账堆重复行）。
    匹配走 json_extract 精确相等——首版用 LIKE '%"spotcheck_of": 6%' 被 65/64 前缀命中
    误判重复（真机实锤），字符串 LIKE 不可做数值匹配。"""
    dup = conn.execute(
        """SELECT id, decision FROM review_decisions
           WHERE reviewer_role='human'
             AND json_extract(evidence, '$.spotcheck_of') = ?
             AND json_extract(evidence, '$.verdict') = ?
           ORDER BY id DESC LIMIT 1""", (decision_id, verdict)).fetchone()
    if dup:
        return {"decision": dup["decision"], "restored": None, "duplicate": True}
    row = conn.execute("SELECT * FROM review_decisions WHERE id=?", (decision_id,)).fetchone()
    if not row:
        raise LookupError(f"决策 {decision_id} 不存在")
    if verdict not in ("uphold", "override"):
        raise ValueError("verdict 必须是 uphold 或 override")
    orig = row["decision"]
    if verdict == "uphold":
        new_decision = orig if orig in ("approve", "reject") else "approve"
        reasoning = f"【抽检同意】{note or '人工抽检确认 AI 决策'}"
    else:
        if orig == "reject":
            new_decision = "approve"
            reasoning = f"【推翻删除】{note or '人工抽检推翻：用例保留'}"
        elif orig == "approve":
            new_decision = "reject"
            reasoning = f"【推翻通过】{note or '人工抽检推翻：用例应删除'}"
        else:  # regenerate/insert 的推翻=不改用例状态，只留对齐信号
            new_decision = "approve"
            reasoning = f"【推翻{orig}】{note or '人工抽检不采纳该增补意见'}"
    conn.execute(
        """INSERT INTO review_decisions(task_id, decision, target, reasoning, evidence,
                                        reviewer_role, round) VALUES (?,?,?,?,?,'human',?)""",
        (row["task_id"], new_decision, row["target"], reasoning[:600],
         json.dumps({"spotcheck_of": decision_id, "verdict": verdict}, ensure_ascii=False),
         row["round"]))
    restored = None
    if verdict == "override" and orig == "reject" and row["target"] != "task":
        conn.execute(
            """UPDATE test_cases SET enabled=1 WHERE enabled=0 AND name=? AND script_id IN
               (SELECT id FROM test_scripts WHERE task_id=?)""", (row["target"], row["task_id"]))
        restored = row["target"]
    conn.commit()
    return {"decision": new_decision, "restored": restored}


def token_budget_exceeded(conn, task_id: int) -> bool:
    """代审停车线（沿用三角色实验先例）：任务级 llm_calls 累计 token 超线即停。"""
    row = conn.execute("SELECT COALESCE(SUM(total_tokens), 0) AS t FROM llm_calls WHERE task_id=?",
                       (task_id,)).fetchone()
    return (row["t"] or 0) >= config.AI_REVIEWER_TOKEN_BUDGET
