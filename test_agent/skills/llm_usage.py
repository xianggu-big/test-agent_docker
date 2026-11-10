"""LLM 调用成本台账（P1-E）：每次真实 LLM 调用的 token 用量落 llm_calls 表。

数据源只认 langchain 标准字段 ``response.usage_metadata``（input_tokens /
output_tokens / total_tokens）；缺失（fake-LLM 单测 / 兼容层 resp）→ 不落行，
诚实不记 0。记账失败（如 FK 违例）吞掉只 warn——成本台账绝不能炸主链路。
skill func 在注册表线程池执行，本模块写库一律自建短连接，不跨线程复用。
"""
import logging

from .. import config
from ..store.db import get_conn

log = logging.getLogger(__name__)


def extract_usage(resp) -> tuple[int, int, int] | None:
    """从 langchain AIMessage 提取 (prompt, completion, total)；无 usage 返回 None。

    total_tokens 缺失时按 prompt+completion 之和补（OpenAI 兼容口均返回三者，
    此兜底只为字段省略的兼容层）；三者全 0 或非数字同样视为无数据。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    try:
        p, c = int(um.get("input_tokens")), int(um.get("output_tokens"))
        t = int(um.get("total_tokens")) if um.get("total_tokens") is not None else p + c
    except (TypeError, ValueError):
        return None
    if p <= 0 and c <= 0:
        return None
    return p, c, t


def is_context_truncated(prompt_tokens: int, base_url: str = "",
                         ctx_budget: int | None = None) -> bool:
    """对账闸（任务卡-H 步骤 2，纯函数）：本地端点实际 prompt_tokens ≥ 预算×0.95 →
    True（KV 溢出 CPU=5 倍掉速，打标供归因与预算治理）。
    预算来源优先级：显式 ctx_budget（make_chat 透传的 profile.ctx_budget，如 local=16384）
    > None（云端档=不限，恒 False）。base_url 仅用于注释语义（预算字段已隐含端点档位）。"""
    if not ctx_budget or ctx_budget <= 0:
        return False
    return prompt_tokens >= int(ctx_budget * 0.95)


def log_llm_call(task_id: int | None, phase: str, resp,
                 model: str | None = None, ctx_budget: int | None = None,
                 base_url: str = "") -> None:
    """单次 LLM 调用记账：自建短连接 INSERT llm_calls；任何情况不上抛。

    task_id 为 None（无任务上下文的调用方，如离线脚本注入的 llm）时跳过。
    ctx_budget/base_url：调用方从 make_chat 产物读（rc._ctx_budget / 内层 base_url），
    传入则启用对账闸（prompt ≥ 预算×0.95 → truncated=1 落库 + warn 留痕）；
    不传=云端档不限，行为与旧行为一致（truncated 恒 0）。
    """
    if task_id is None:
        return
    u = extract_usage(resp)
    if not u:
        return
    truncated = 1 if is_context_truncated(u[0], base_url, ctx_budget) else 0
    if truncated:
        log.warning("[ctx-budget] phase=%s prompt=%d ≥ 预算 %d×0.95 —— 本地 KV 溢出"
                    "（5 倍掉速），建议：减小源码预算或分模块生成", phase, u[0], ctx_budget)
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO llm_calls(task_id, phase, prompt_tokens,"
                " completion_tokens, total_tokens, model, truncated)"
                " VALUES (?,?,?,?,?,?,?)",
                (task_id, phase, u[0], u[1], u[2], model or config.DEEPSEEK_MODEL,
                 truncated))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:              # noqa: BLE001 —— 记账失败不上抛，主链路优先
        log.warning("llm_calls 记账失败 task=%s phase=%s: %s", task_id, phase, e)
