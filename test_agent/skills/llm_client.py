"""LLM 客户端统一入口（B5 / 2026-11-02）：退避重试 + 余额耗尽友好停机。

问题（评估实证）：DeepSeek 402（Insufficient Balance）期间各调用点各自重试或直接抛，
任务被记成 failed 且看不出"是钱没了不是代码坏了"；429/超时也各写各的。

用法（替换 `ChatOpenAI(...)` 为 `make_chat(...)`，参数完全一致）：
    from .llm_client import make_chat
    llm = make_chat(model=..., api_key=..., base_url=..., temperature=0.2, timeout=90)
    resp = llm.invoke([...])          # 调用方代码零改动

行为：
- 402 / "Insufficient Balance" → 抛 BudgetExhausted（RuntimeError 子类，中文可行动文案），
  **不重试**（再试也是白烧时间）；MTA_LLM_FALLBACK=1 时先尝试本地备胎
  （config.LLM_FALLBACK_*，产物 additional_kwargs.llm_fallback 打标；备胎不可达则照常抛）；
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

    def _try_fallback(self, messages, *a, **kw):
        """余额耗尽时的本地备胎（MTA_LLM_FALLBACK=1 开启，默认关=旧行为逐字节不变）。

        诚实纪律三条：
        1. 降级产物必须在 additional_kwargs.llm_fallback 打标（哪个模型救的场），
           台账/调用方可识别——防备胎产物混进 DeepSeek 口径的实验数据；
        2. 备胎不可达/自身失败 → 返回 None，原 402 的 BudgetExhausted 照常抛，
           绝不把"备胎挂了"变形吞掉；
        3. 只对 API 侧余额耗尽生效；自家 token 停车线的预算停止是实验预注册边界，
           不走这里（若停车线直接 raise BudgetExhausted 也不经过 invoke 的 budget 分支）。
        """
        from .. import config
        if not getattr(config, "LLM_FALLBACK_ENABLED", False):
            return None
        try:
            from .llm_profile import current
            if current().get("provider") == "local":
                log.info("[llm-fallback] 当前 profile 已是本地模型，跳过备胎（同端点降级无意义）")
                return None
        except Exception:             # noqa: BLE001 —— profile 读取失败不改变备胎原有行为
            pass
        try:
            from langchain_openai import ChatOpenAI
            log.warning("[llm-fallback] 主模型余额耗尽，降级本地 %s@%s（phase=%s task=%s）",
                        config.LLM_FALLBACK_MODEL, config.LLM_FALLBACK_BASE_URL,
                        self._phase or "-", self._task_id or "-")
            fb = ChatOpenAI(model=config.LLM_FALLBACK_MODEL, api_key="ollama",
                            base_url=config.LLM_FALLBACK_BASE_URL,
                            temperature=kw.get("temperature", 0.2),
                            timeout=config.LLM_FALLBACK_TIMEOUT, max_retries=1,
                            max_tokens=config.LLM_FALLBACK_MAX_TOKENS)
            resp = fb.invoke(messages, *a, **kw)
            try:
                base = dict(getattr(resp, "additional_kwargs", {}) or {})
                base["llm_fallback"] = {"model": config.LLM_FALLBACK_MODEL,
                                        "reason": "primary_budget_exhausted"}
                resp.additional_kwargs = base
            except Exception:             # noqa: BLE001 —— 标记失败不吞降级结果
                log.warning("[llm-fallback] 降级结果打标失败（产物照常返回，台账将缺备胎标记）")
            log.warning("[llm-fallback] 备胎产出 %s 字符", len(getattr(resp, "content", "") or ""))
            return resp
        except Exception as e:            # noqa: BLE001 —— 备胎任何失败都不改变原错误语义
            log.error("[llm-fallback] 本地备胎不可用：%s: %s（原 402 照常抛）",
                      type(e).__name__, str(e)[:160])
            return None

    def invoke(self, messages, *a, **kw):
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                return self._inner.invoke(messages, *a, **kw)
            except Exception as e:        # noqa: BLE001 —— 分类后再决定抛/重试
                kind = _classify(e)
                if kind == "budget":
                    fb = self._try_fallback(messages, *a, **kw)
                    if fb is not None:
                        return fb
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
    """构造带退避的 ChatOpenAI（参数与 ChatOpenAI 完全一致；延迟导入避免无 Key 场景开销）。

    LLM 三件套兜底（任务卡-LLM模型选择-20261108）：model/base_url/api_key 未显式传时
    取**当前 profile 真源（app_settings 表，current()）**——674/675 实锤：apply 的运行时
    改写只发生在执行 apply 的那个进程，长驻 server 的内存 config 会与表漂移
    （表已切回 deepseek、server 内存仍是 qwen 的 DashScope → deepseek key 发错端点
    401）——兜底必须读表即真源，不读进程内存。**显式传参仍优先**——逃生口：
    ai_reviewer 的 per-role 模型覆盖零改动。"""
    from langchain_openai import ChatOpenAI
    phase = kwargs.pop("phase", "")
    task_id = kwargs.pop("task_id", None)
    retries = kwargs.pop("retries", 3)
    from .. import config
    try:
        # 2026-11-10 每节点 LLM 可替换：phase 命中 routing 组则用该 profile，否则
        # default 档（routing 空/全指 default 时与原 current() 行为逐字节一致）；
        # 显式传参仍优先——逃生口（ai_reviewer per-role 覆盖零改动）
        from .llm_profile import resolve_phase
        p = resolve_phase(phase)
        kwargs.setdefault("model", p.get("model") or config.DEEPSEEK_MODEL)
        kwargs.setdefault("base_url", p.get("base_url") or config.DEEPSEEK_BASE_URL)
        if not kwargs.get("api_key"):
            from .llm_profile import resolve_key
            kwargs["api_key"] = resolve_key(p) or ""
    except Exception:                     # noqa: BLE001 —— profile 读取失败回退 config 默认
        kwargs.setdefault("model", config.DEEPSEEK_MODEL)
        kwargs.setdefault("base_url", config.DEEPSEEK_BASE_URL)
        kwargs.setdefault("api_key", "")
    # 响应强制按 utf-8 解码（2026-11-05 批4 实锤：DeepSeek 部分 CDN 节点响应头
    # charset=gbk → httpx 尊重 header 解码 → 中文全部 mojibake 且 U+FFFD 丢字节
    # 无法事后逆向；DeepSeek 输出恒为 utf-8，忽略 header charset 是安全的）
    if "http_client" not in kwargs:
        import httpx
        kwargs["http_client"] = httpx.Client(default_encoding="utf-8")
    return RobustChat(ChatOpenAI(**kwargs), retries=retries, phase=phase, task_id=task_id)
