"""understand：需求理解与提炼层（任意形态输入 → 结构化测试方向/目标，2026-11-08）。

定位：入口归一化——一句话/小白话/任务书等任意形态的 requirement，先提炼出
测试方向/目标/范围提示/显式假设，给 plan 节点当上下文（信息提供层），再走既有
"拆条件→锚真值清单→生成"链路。四条纪律：
1. 产物落痕可审计（调用方负责写 agent_messages，本模块只产出）；
2. 产物是策略不是条件——test_goals 只当上下文，测试条件仍必须锚真值清单（闸不放松）；
3. fail-open：解析失败/空输出/旗标关 → 返回 None，调用方透传原文不拦主链路；
4. 假设显式化：输入含糊处输出 assumptions，不许 LLM 憋着猜。
"""
import json
import re

from .. import config


def _parse_llm_json(text: str):
    """剥 ```json 围栏后解析；失败返回 None（诚实，不猜）。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def _str_list(v) -> list[str]:
    """宽松归一：None→[]；标量→[标量]；列表逐项转 str，丢弃 None/空/null 字符串。"""
    if not v:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        out = []
        for x in v:
            if x is None:
                continue
            txt = str(x).strip()
            if txt and txt.lower() != "none":
                out.append(txt)
        return out
    txt = str(v).strip()
    return [txt] if txt and txt.lower() != "none" else []


def understand_requirement(api_key: str, raw_text: str, project_hint: str = "",
                           llm_factory=None, on_resp=None):
    """任意形态需求 → 结构化理解产物；fail-open：任何失败返回 None（调用方透传原文）。

    返回 {"test_goals": [...], "scope_hints": [...], "test_approaches": [...],
          "assumptions": [...]}；llm_factory 供测试注入（默认 make_chat）；
    on_resp(resp) 审计回调（调用方落 llm_calls 台账——token 记账完整性）。
    """
    if not config.UNDERSTAND_ENABLED:
        return None
    raw = (raw_text or "").strip()
    if not raw:
        return None
    from .llm_client import make_chat
    factory = llm_factory or (lambda **kw: make_chat(**kw))
    chat = factory(api_key=api_key, temperature=0,
                   timeout=90, max_tokens=2000, phase="understand")
    sysmsg = (
        "你是测试需求理解器。输入可能是：一句话需求、非专业用户的口语描述、或一份完整的"
        "测试任务书（含角色规范/交付物清单/执行命令等）。你的任务：提炼出对**被测系统**的"
        "测试方向与目标。区分规则：描述被测系统在给定输入下的输出或状态变化的句子才是测试"
        "内容；描述测试工程本身的部分（生成报告/执行命令/交付格式/行为约束）是任务规范，"
        "不是测试内容——最多把它们中可执行的语义归入 test_approaches。输入含糊处必须写进"
        "assumptions（显式假设），不要憋着猜。只输出 JSON："
        '{"test_goals":["..."],"scope_hints":["..."],"test_approaches":["..."],'
        '"assumptions":["..."]}，不要任何解释。')
    try:
        resp = chat.invoke([
            ("system", sysmsg),
            ("user", (f"被测项目提示：{project_hint or '（未知，按需求文本推断）'}\n\n"
                      f"原始输入：\n{raw[:12000]}")),
        ])
        if on_resp:
            try:
                on_resp(resp)
            except Exception:      # noqa: BLE001 —— 审计失败不拦主链路
                pass
        data = _parse_llm_json(getattr(resp, "content", "") or "")
        if not isinstance(data, dict):
            return None
        out = {
            "test_goals": _str_list(data.get("test_goals"))[:10],
            "scope_hints": _str_list(data.get("scope_hints"))[:10],
            "test_approaches": _str_list(data.get("test_approaches"))[:10],
            "assumptions": _str_list(data.get("assumptions"))[:10],
        }
        if not out["test_goals"]:
            return None            # 没提炼出任何方向 = 理解失败（诚实，不硬造）
        return out
    except Exception:              # noqa: BLE001 —— fail-open：理解层挂了不拦主链路
        return None


def render_understanding(u: dict) -> str:
    """理解产物 → plan 提示词注入段（信息提供：供拆条件对齐，不替代真值锚定）。"""
    if not u:
        return ""
    blocks = []
    for key, label in (("test_goals", "测试方向/目标"), ("scope_hints", "范围提示"),
                       ("test_approaches", "测试方法倾向"), ("assumptions", "显式假设")):
        items = u.get(key) or []
        if items:
            blocks.append(label + "：" + "；".join(items))
    if not blocks:
        return ""
    return ("\n\n## 需求理解（理解层提炼，供拆条件对齐）\n"
            "以下是对原始输入的理解提炼——条件应与这些方向对齐；「显式假设」若成立"
            "必须在对应 condition 中沿用注明：\n"
            + "\n".join(f"- {b}" for b in blocks))
