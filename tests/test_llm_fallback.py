"""LLM 本地备胎（llm_client._try_fallback）的机制单测。

全部用假 inner/fake ChatOpenAI，不碰真 Ollama。纪律：默认关=旧行为逐字节不变。
"""
import pytest
from langchain_core.messages import AIMessage

from test_agent import config
from test_agent.skills.llm_client import BudgetExhausted, RobustChat


class _BudgetBoom:
    """主模型：恒抛 402 类错误。"""

    model_name = "deepseek-chat"

    def invoke(self, messages, *a, **kw):
        raise RuntimeError("Error code: 402 - Insufficient Balance")


class _FakeFallbackLLM:
    """备胎替身：记录构造参数，invoke 可配置成功/失败。"""

    last_kwargs = None
    behavior = "ok"

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def invoke(self, messages, *a, **kw):
        if type(self).behavior != "ok":
            raise RuntimeError("connection refused (ollama down)")
        return AIMessage(content="fallback json", additional_kwargs={})


@pytest.fixture(autouse=True)
def _patch_fb_llm(monkeypatch):
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeFallbackLLM)
    _FakeFallbackLLM.behavior = "ok"
    _FakeFallbackLLM.last_kwargs = None                 # 类属性跨测试污染防御
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "qwen3-4b-inst-test")
    # 备胎互斥（2026-11-08）按"当前 profile=local"短路——本文件测的是非 local 档行为，
    # 显式 mock current=deepseek，不依赖真实库 app_settings 的档位（用户随时可切）
    from test_agent.skills import llm_profile
    monkeypatch.setattr(llm_profile, "current", lambda conn=None: {
        **llm_profile.PRESETS["deepseek"], "updated_by": "", "updated_at": ""})
    yield


def test_flag_off_keeps_old_behavior(monkeypatch):
    """默认关：402 照常抛 BudgetExhausted，绝不碰备胎。"""
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", False)
    chat = RobustChat(_BudgetBoom(), retries=1)

    with pytest.raises(BudgetExhausted):
        chat.invoke([("user", "hi")])
    assert _FakeFallbackLLM.last_kwargs is None          # 备胎从未被构造


def test_flag_on_falls_back_and_marks(monkeypatch):
    """开启：402 → 备胎接手，产物带 llm_fallback 标记（诚实纪律）。"""
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    chat = RobustChat(_BudgetBoom(), retries=1)

    resp = chat.invoke([("user", "hi")])

    assert resp.content == "fallback json"
    assert resp.additional_kwargs["llm_fallback"]["model"] == "qwen3-4b-inst-test"
    assert resp.additional_kwargs["llm_fallback"]["reason"] == "primary_budget_exhausted"
    assert _FakeFallbackLLM.last_kwargs["base_url"] == config.LLM_FALLBACK_BASE_URL


def test_flag_on_fallback_down_raises_original_budget(monkeypatch):
    """备胎不可达：不吞错不变形，原 402 的 BudgetExhausted 照常抛。"""
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)
    _FakeFallbackLLM.behavior = "down"
    chat = RobustChat(_BudgetBoom(), retries=1)

    with pytest.raises(BudgetExhausted):
        chat.invoke([("user", "hi")])


def test_fallback_only_on_budget_not_on_other_errors(monkeypatch):
    """只对余额类降级：限流/致命错误不走备胎（限流重试耗尽→RateLimited）。"""
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)

    class _FatalBoom:
        model_name = "deepseek-chat"

        def invoke(self, messages, *a, **kw):
            raise ValueError("bad parameter")            # fatal 类

    with pytest.raises(ValueError):
        RobustChat(_FatalBoom(), retries=1).invoke([("user", "hi")])
    assert _FakeFallbackLLM.last_kwargs is None

    class _RateBoom:
        model_name = "deepseek-chat"

        def invoke(self, messages, *a, **kw):
            raise RuntimeError("429 too many requests")  # retry 类

    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="连续"):
        RobustChat(_RateBoom(), retries=1).invoke([("user", "hi")])
    assert _FakeFallbackLLM.last_kwargs is None


def test_budget_marking_failure_still_returns(monkeypatch):
    """打标失败不吞降级结果（诚实标记尽力而为，产物价值优先）。"""
    monkeypatch.setattr(config, "LLM_FALLBACK_ENABLED", True)

    class _FrozenAIMessage:
        content = "fallback"
        additional_kwargs = None                        # 赋值会炸的对象

        def __setattr__(self, k, v):
            raise RuntimeError("frozen")

    class _FrozenFB:
        last_kwargs = None

        def __init__(self, **kw):
            pass

        def invoke(self, messages, *a, **kw):
            return _FrozenAIMessage()

    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FrozenFB)
    chat = RobustChat(_BudgetBoom(), retries=1)

    resp = chat.invoke([("user", "hi")])
    assert resp.content == "fallback"                   # 产物照常返回
