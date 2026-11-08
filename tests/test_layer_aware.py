# -*- coding: utf-8 -*-
"""层位感知生成（2026-11-08 653/652 实锤：注入 bug 在错误映射中间件，AI 用例全落
service 层手写 stub——stub 世界不含中间件=bug 在测试执行链路里根本不运行；D2 反馈
指了方向 AI 仍落错层——层位信息必须显式提供，LLM 不会自发升层）：
提示词注入层位覆盖纪律 + 按语言附 HTTP 层用例骨架（go=httptest / python=TestClient）。
旗标 MTA_LAYER_AWARE（LAYER_AWARE_ENABLED）默认关=提示词逐字节不变（基线保护）。"""
from test_agent import config
from test_agent.skills.prompt_library import build_user_prompt

_SRC = [("calc.py", "def add(a, b):\n    return a + b\n")]


# ---------- 旗标默认关（基线保护） ----------

def test_flag_默认关():
    import os
    saved = os.environ.pop("MTA_LAYER_AWARE", None)
    try:
        import importlib
        cfg = importlib.reload(config)
        assert cfg.LAYER_AWARE_ENABLED is False
    finally:
        if saved is not None:
            os.environ["MTA_LAYER_AWARE"] = saved
        importlib.reload(config)


# ---------- 开关两态：关=逐字节不变 ----------

def test_off_默认不注入():
    base = build_user_prompt(_SRC, "需求")
    assert "层位覆盖纪律" not in base
    assert "httptest" not in base and "TestClient" not in base
    # 显式 False 与默认逐字节一致
    assert base == build_user_prompt(_SRC, "需求", layer_aware=False)


# ---------- 开=语言分支 ----------

def test_on_go_注入纪律与_httptest_骨架():
    p = build_user_prompt(_SRC, "需求", target_language="go", layer_aware=True)
    assert "层位覆盖纪律" in p
    assert "HTTP 层用例" in p                      # 分布要求第 1 条
    assert "httptest.NewRequest" in p and "ServeHTTP" in p   # 标准库骨架
    assert "从被测源码" in p                        # 防幻觉：构造从源码定位
    assert "禁止把尖括号" in p                      # 占位照抄禁令
    assert "stub 世界里根本不会运行" in p            # 653 根因的机制解释


def test_on_python_注入_TestClient_骨架():
    p = build_user_prompt(_SRC, "需求", target_language="python", layer_aware=True)
    assert "层位覆盖纪律" in p
    assert "TestClient" in p
    assert "httptest" not in p                     # Go 骨架不泄漏


def test_on_python_默认语言形态同注入():
    # lang 未显式传（Python 项目默认 ""）时同样给 Python 骨架
    p = build_user_prompt(_SRC, "需求", layer_aware=True)
    assert "TestClient" in p and "层位覆盖纪律" in p


def test_on_js_仅通用纪律_无语言骨架():
    p = build_user_prompt(_SRC, "需求", target_language="javascript", layer_aware=True)
    assert "层位覆盖纪律" in p
    assert "httptest" not in p and "TestClient" not in p   # 无 JS 骨架（依赖不定）


def test_on_注入位置_条件段之前():
    # 层位纪律应出现在测试条件段之前（先层位策略后逐条对准）
    p = build_user_prompt(_SRC, "需求", layer_aware=True,
                          conditions=[{"condition": "条件1"}])
    assert p.index("层位覆盖纪律") < p.index("测试条件")


# ---------- gen_tests 接线（旗标读 config，模块级传递） ----------

def test_gen_tests_接线_旗标读config(monkeypatch):
    import inspect
    from test_agent.skills import gen_tests
    src = inspect.getsource(gen_tests)
    assert "layer_aware=config.LAYER_AWARE_ENABLED" in src   # 主轮接线存在
    # refill 补生成轮保持自由通道（单变量纪律：验证有效后再扩展）
    assert src.count("layer_aware=config.LAYER_AWARE_ENABLED") == 1
