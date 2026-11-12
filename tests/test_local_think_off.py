"""本地思考型模型的思考开关注入（2026-11-12）。

背景：Ollama 自 0.35 起把 qwen3.5 的 renderer 默认设为 thinking on，而
/­v1 OpenAI 兼容端点 **忽略 `think: false`**，只有 `reasoning_effort="none"`
生效。实测（本机 3050 Ti / Ollama 0.40.16）：
  不传参        → 67.8s、烧满 3000 token、content 为空、_parse_cases 判废
  reasoning_effort="none" → 28.4s、正文 4063 字符、6 条用例解析通过
Modelfile 里 `PARAMETER think false` 报 unknown parameter，系统提示塞 /no_think 也无效，
所以只能在调用侧注入。本组用例锁三件事：命中范围、端点边界、不覆盖调用方显式设置。
"""
from test_agent.skills.llm_client import (
    _apply_local_think_off,
    _is_local_endpoint,
    _thinking_model,
)


def test_thinking_model_hits_qwen35_family():
    for m in ("qwen3.5:4b", "qwen35-4b-text", "qwen3.6-27b", "qwen38:latest"):
        assert _thinking_model(m) is True, m


def test_thinking_model_misses_non_thinking_and_cloud_models():
    # qwen3 无思考变体（现役备胎）、deepseek、空串都不能误命中
    for m in ("qwen3-4b-inst-test", "qwen3:4b", "deepseek-chat", "qwen-plus", "", None):
        assert _thinking_model(m or "") is False, m


def test_is_local_endpoint_boundaries():
    for u in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1",
              "http://192.168.1.9:11434/v1"):
        assert _is_local_endpoint(u) is True, u
    for u in ("https://api.deepseek.com", "https://dashscope.aliyuncs.com/compatible-mode/v1", ""):
        assert _is_local_endpoint(u) is False, u


def test_injects_reasoning_effort_for_local_thinking_model():
    kw = {"model": "qwen35-4b-text", "base_url": "http://localhost:11434/v1"}
    _apply_local_think_off(kw)
    assert kw["model_kwargs"]["reasoning_effort"] == "none"


def test_skips_cloud_endpoint_even_if_model_name_matches():
    """云端对未声明参数的容忍度不一致——同名模型走云端时不注入。"""
    kw = {"model": "qwen3.5:4b", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
    _apply_local_think_off(kw)
    assert "model_kwargs" not in kw


def test_skips_non_thinking_model_on_local():
    kw = {"model": "qwen3-4b-inst-test", "base_url": "http://localhost:11434/v1"}
    _apply_local_think_off(kw)
    assert "model_kwargs" not in kw


def test_respects_caller_explicit_setting():
    """调用方显式指定时必须原样保留（逃生口），不被 setdefault 逻辑覆盖。"""
    kw = {"model": "qwen35-4b-text", "base_url": "http://localhost:11434/v1",
          "model_kwargs": {"reasoning_effort": "high", "top_p": 0.9}}
    _apply_local_think_off(kw)
    assert kw["model_kwargs"]["reasoning_effort"] == "high"
    assert kw["model_kwargs"]["top_p"] == 0.9
