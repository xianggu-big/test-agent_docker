"""LLM 客户端统一入口（B5 / 2026-11-02）：退避重试 + 余额耗尽友好停机。

问题（评估实证）：DeepSeek 402（Insufficient Balance）期间各调用点各自重试或直接抛，
任务被记成 failed 且看不出"是钱没了不是代码坏了"；429/超时也各写各的。

用法（替换 `ChatOpenAI(...)` 为 `make_chat(...)`，参数完全一致）：
    from .llm_client import make_chat
    llm = make_chat(model=..., api_key=..., base_url=..., temperature=0.2, timeout=90)
    resp = llm.invoke([...])          # 调用方代码零改动

行为：
- 402 / "Insufficient Balance" → 抛 BudgetExhausted（RuntimeError 子类，中文可行动文案），
  **不重试**（再试也是白烧时间）；
- 429 / 5xx / 超时 / 连接错误 → 指数退避重试（默认 3 次，2s→4s→8s），最后一次仍失败原样抛；
- 其它错误（400 参数错等）→ 直接抛（重试无意义）。
"""
import logging
import time

log = logging.getLogger(__name__)


class BudgetExhausted(RuntimeError):
    """LLM 余额耗尽（402）。消息面向用户，可直接展示。"""


class RateLimited(RuntimeError):
    """限流/服务端错误重试后仍失败。"""


_RETRYABLE_HINTS = ("429", "rate limit", "rate_limit", "too many requests",
                    "500", "502", "503", "504", "timeout", "timed out",
                    "connection", "temporarily", "overloaded")
_BUDGET_HINTS = ("402", "insufficient balance", "insufficient_quota",
                 "exceeded your current quota", "billing")


def _classify(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(h in text for h in _BUDGET_HINTS):
        return "budget"
    if any(h in text for h in _RETRYABLE_HINTS):
        return "retry"
    return "fatal"


class RobustChat:
    """ChatOpenAI 的薄包装：invoke 带退避；属性透传（model/model_name 等按需读）。"""

    def __init__(self, inner, *, retries: int = 3, base_delay: float = 2.0,
                 phase: str = "", task_id: int | None = None):
        self._inner = inner
        self._retries = retries
        self._base = base_delay
        self._phase = phase
        self._task_id = task_id

    def __getattr__(self, name):          # 透传 model / model_name / temperature 等
        return getattr(self._inner, name)

    def invoke(self, messages, *a, **kw):
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                return self._inner.invoke(messages, *a, **kw)
            except Exception as e:        # noqa: BLE001 —— 分类后再决定抛/重试
                kind = _classify(e)
                if kind == "budget":
                    raise BudgetExhausted(
                        "大模型余额不足（402 Insufficient Balance）：请充值后重试；"
                        "本任务未消耗后续轮次，当前结果已如实保存。"
                        f"（原始信息：{type(e).__name__}: {e}）") from e
                if kind == "fatal":
                    raise
                last = e
                wait = self._base * (2 ** attempt)
                log.warning("LLM 调用失败（%s%s），%.0fs 后重试 %d/%d：%s",
                            type(e).__name__, f" phase={self._phase}" if self._phase else "",
                            wait, attempt + 1, self._retries, str(e)[:120])
                if attempt < self._retries - 1:
                    time.sleep(wait)
        raise RateLimited(
            f"大模型服务连续 {self._retries} 次不可用（限流/超时/服务端错误）："
            f"{type(last).__name__}: {last}") from last


def make_chat(**kwargs):
    """构造带退避的 ChatOpenAI（参数与 ChatOpenAI 完全一致；延迟导入避免无 Key 场景开销）。"""
    from langchain_openai import ChatOpenAI
    phase = kwargs.pop("phase", "")
    task_id = kwargs.pop("task_id", None)
    retries = kwargs.pop("retries", 3)
    return RobustChat(ChatOpenAI(**kwargs), retries=retries, phase=phase, task_id=task_id)
