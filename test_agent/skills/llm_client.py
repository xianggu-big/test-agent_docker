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

    def _try_fallback(self, messages, *a, reason: str = "primary_budget_exhausted", **kw):
        """DeepSeek 用不了时的本地备胎（MTA_LLM_FALLBACK=1 开启，默认关=旧行为逐字节不变）。

        覆盖两种"用不了"（2026-11-15 用户拍板：本地 LLM 仅在 DeepSeek 不可用时兜底）：
        1. 余额耗尽（402 → BudgetExhausted 分支，reason=primary_budget_exhausted）；
        2. 连接类错误退避穷尽（APIConnectionError/超时/5xx → RateLimited 分支，
           reason=primary_unreachable——2026-11-15 实锤：E9 wcwidth 批 21 格 gen 三连挂
           生成 0 条，备胎只认 402 没接住连接错误）。

        诚实纪律三条：
        1. 降级产物必须在 additional_kwargs.llm_fallback 打标（哪个模型救的场），
           台账/调用方可识别——防备胎产物混进 DeepSeek 口径的实验数据；
        2. 备胎不可达/自身失败 → 返回 None，原错误照常抛，
           绝不把"备胎挂了"变形吞掉；
        3. 只对 API 侧不可用生效；自家 token 停车线的预算停止是实验预注册边界，
           不走这里（若停车线直接 raise BudgetExhausted 也不经过 invoke 的 budget 分支）。
        """
        from .. import config
        if not getattr(config, "LLM_FALLBACK_ENABLED", False):
            return None
        try:
            # 2026-11-10 升级（任务卡-B 设计 5）：按路由后的实际端点判断——
            # phase 路由到 local 而.default=deepseek 时，旧"看 current 档位"判据会漏
            from .llm_profile import current_doc, resolve_phase
            rp = resolve_phase(self._phase or "")
            fb_host = "localhost:11434" in (config.LLM_FALLBACK_BASE_URL or "")
            main_host = "localhost:11434" in (rp.get("base_url") or "")
            if main_host and (fb_host or (current_doc().get("profiles") or {})
                              .get(rp.get("provider"), {}).get("provider") == "local"):
                log.info("[llm-fallback] 主模型已是本地端点（phase=%s → %s），"
                         "跳过备胎（同端点降级无意义）", self._phase or "-",
                         rp.get("base_url"))
                return None
        except Exception:             # noqa: BLE001 —— 判定失败不改变备胎原有行为
            pass
        try:
            from langchain_openai import ChatOpenAI
            log.warning("[llm-fallback] 主模型余额耗尽，降级本地 %s@%s（phase=%s task=%s）",
                        config.LLM_FALLBACK_MODEL, config.LLM_FALLBACK_BASE_URL,
                        self._phase or "-", self._task_id or "-")
            # 构造与主路径同款三件套（2026-11-15 闲时找bug 对齐）：
            # ①思考型备胎必须关思考（模块自证：不传 content 恒空）；
            # ②utf-8 http_client（CDN gbk mojibake 修复未覆盖备胎）；
            # ③temperature 透传原构造值（invoke 期 kwargs 不含构造参数，旧恒 0.2）
            fb_kwargs = dict(model=config.LLM_FALLBACK_MODEL, api_key="ollama",
                             base_url=config.LLM_FALLBACK_BASE_URL,
                             temperature=getattr(self._inner, "temperature", 0.2),
                             timeout=config.LLM_FALLBACK_TIMEOUT, max_retries=1,
                             max_tokens=config.LLM_FALLBACK_MAX_TOKENS)
            _apply_local_think_off(fb_kwargs)
            fb_kwargs.setdefault("http_client", __import__("httpx").Client(
                default_encoding="utf-8"))
            fb = ChatOpenAI(**fb_kwargs)
            resp = fb.invoke(messages, *a, **kw)
            try:
                base = dict(getattr(resp, "additional_kwargs", {}) or {})
                base["llm_fallback"] = {"model": config.LLM_FALLBACK_MODEL,
                                        "reason": reason}
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
                # 连接类错误兜底（2026-11-15 拍板落地）：重试穷尽=DeepSeek 此刻用不了，
                # 与 402 同走本地备胎（打标 reason=primary_unreachable）——E9 wcwidth
                # 批 21 格 gen 三连挂生成 0 条的实锤修法
                if attempt == self._retries - 1:
                    fb = self._try_fallback(messages, *a, **kw,
                                            reason="primary_unreachable")
                    if fb is not None:
                        return fb
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


# qwen3.5+ 系列默认开思考，而 Ollama 的 OpenAI 兼容端点 **不认 `think: false`**
# （2026-11-12 实测：不传参 67.8s 烧满 3000 token、content 为空、_parse_cases 判废；
#   传 reasoning_effort="none" 则 28.4s 正常产出 6 条用例）。Modelfile 里
# `PARAMETER think false` 会报 unknown parameter，系统提示塞 /no_think 也无效——
# 关闭思考只有调用侧这一条路，且本地/云端开关名不同。
# 只对**本地端点**注入：云端供应商对未声明参数的容忍度不一致，不碰。
_THINKING_MODEL_PREFIXES = ("qwen3.5", "qwen35", "qwen3.6", "qwen36", "qwen3.8", "qwen38")


def _is_local_endpoint(base_url: str) -> bool:
    u = (base_url or "").lower()
    return "localhost" in u or "127.0.0.1" in u or ":11434" in u


def _thinking_model(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _THINKING_MODEL_PREFIXES)


def _apply_local_think_off(kwargs: dict) -> None:
    """给本地思考型模型补 reasoning_effort="none"；调用方显式设过则原样尊重。"""
    if not (_thinking_model(kwargs.get("model", "")) and
            _is_local_endpoint(kwargs.get("base_url", ""))):
        return
    mk = dict(kwargs.get("model_kwargs") or {})
    mk.setdefault("reasoning_effort", "none")
    kwargs["model_kwargs"] = mk


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
        # G 系统臂 718~720 实锤：profile.timeout=该端点请求超时**下限**——调用点写死的
        # 60/90/120 对 ollama（生成 ~12 tok/s）必超时，取 max(调用点值, profile 值)；
        # 云端预设无该字段=调用点值原样生效（行为逐字节不变）
        ptimeout = int(p.get("timeout") or 0)
        if ptimeout:
            kwargs["timeout"] = max(int(kwargs.get("timeout") or 0), ptimeout)
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
    # 本地思考型模型（qwen3.5+）必须显式关思考，否则 content 恒空——见上方常量注释
    _apply_local_think_off(kwargs)
    if "http_client" not in kwargs:
        import httpx
        kwargs["http_client"] = httpx.Client(default_encoding="utf-8")
    rc = RobustChat(ChatOpenAI(**kwargs), retries=retries, phase=phase, task_id=task_id)
    # 任务卡-H 步骤 2/3：路由后 profile 的 ctx_budget 与实际端点透传给调用方——
    # log_llm_call 的对账闸与 build_user_prompt 的预算降采样都从这里读
    try:
        rc._ctx_budget = int(p.get("ctx_budget") or 0) or None
    except Exception:                 # noqa: BLE001 —— 无 ctx_budget=不限（云端档）
        rc._ctx_budget = None
    rc._base_url = str(kwargs.get("base_url") or "")
    return rc
