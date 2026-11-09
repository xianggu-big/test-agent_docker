# -*- coding: utf-8 -*-
"""key 闸统一 + 空集归因如实（2026-11-09，任务卡-LLM模型选择 675 同族缺陷修复）。

688/689/690 实锤：local profile（ollama，无需 key）下 builtin.gen_tests 仍硬编码读
os.environ["DEEPSEEK_API_KEY"] → 两轮"未配置 DEEPSEEK_API_KEY：跳过用例生成" →
出题 0 条。修复=8 处 key 闸统一 llm_profile.effective_key()（显式传参优先 → 当前
profile key → 兜底 None；local 恒 "ollama"）+ 空集提示按 skill 实际 reason 归因。

防回归锁采用两类：
1. 行为锁：builtin.gen_tests/log_analyze、preflight 直调（monkeypatch llm_profile）；
2. 源码锁：8 个改动文件不得再出现 os.environ.get("DEEPSEEK_API_KEY") 硬闸
   （effective_key 内部读环境变量属合法实现，不在锁内）。
"""
import json
from pathlib import Path

import pytest

from test_agent import config
from test_agent.skills import llm_profile


@pytest.fixture(autouse=True)
def _restore_config():
    saved = (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL)
    yield
    config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL = saved


def _use_local_profile(monkeypatch):
    monkeypatch.setattr(llm_profile, "current", lambda conn=None: {
        **llm_profile.PRESETS["local"], "updated_by": "", "updated_at": ""})


def _use_qwen_profile_no_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(llm_profile, "current", lambda conn=None: {
        **llm_profile.PRESETS["qwen"], "updated_by": "", "updated_at": ""})


# ---------- A：builtin.gen_tests / log_analyze 行为锁 ----------

def test_gen_tests_local档不再跳过(monkeypatch, tmp_path):
    _use_local_profile(monkeypatch)
    from test_agent.skills import builtin
    called = {}
    import test_agent.skills.gen_tests as _gt
    def _fake_gen(api_key, args, tid, runner):
        called["key"] = api_key
        return '{"generated": 1}'
    monkeypatch.setattr(_gt, "generate_tests", _fake_gen)
    out = builtin.build_skills(runner=object())["gen_tests"].func(
        1, {"workspace": str(tmp_path)})
    assert json.loads(out)["generated"] == 1
    assert called["key"] == "ollama"            # local 恒 ollama，不再误判无 Key


def test_gen_tests_qwen无key如实降级且文案带profile(monkeypatch, tmp_path):
    _use_qwen_profile_no_key(monkeypatch)
    from test_agent.skills import builtin
    out = builtin.build_skills(runner=object())["gen_tests"].func(
        1, {"workspace": str(tmp_path)})
    d = json.loads(out)
    assert d["generated"] == 0 and d["skip_reason"] == "no_key"
    assert "profile=qwen" in d["reason"] and "DASHSCOPE_API_KEY" in d["reason"]
    assert "DEEPSEEK_API_KEY" not in d["reason"]        # 不再指向错误的环境变量


def test_log_analyze_local档不再跳过(monkeypatch):
    _use_local_profile(monkeypatch)
    from test_agent.skills import builtin
    out = builtin.build_skills(runner=object())["log_analyze"].func(1, {})
    d = json.loads(out)
    # 不再被判无 Key——skipped 只能是"无日志证据"这类业务原因，不得含 Key 字样
    assert "Key" not in d.get("skipped", "") and "key" not in d.get("skipped", "")


def test_log_analyze_qwen无key文案(monkeypatch):
    _use_qwen_profile_no_key(monkeypatch)
    from test_agent.skills import builtin
    out = builtin.build_skills(runner=object())["log_analyze"].func(1, {})
    d = json.loads(out)
    assert "profile=qwen" in d["skipped"] and "DASHSCOPE_API_KEY" in d["skipped"]


# ---------- A：preflight 行为锁 ----------

def test_preflight_local档key项ok(monkeypatch):
    _use_local_profile(monkeypatch)
    from test_agent.sandbox import preflight
    out = preflight.probe()
    llm = next(i for i in out["items"] if i["label"] == "大模型 API Key")
    assert llm["status"] == "ok"
    assert "无需 Key" in llm["detail"] and "local" in llm["detail"]


def test_preflight_qwen无key文案带profile(monkeypatch):
    _use_qwen_profile_no_key(monkeypatch)
    from test_agent.sandbox import preflight
    out = preflight.probe()
    llm = next(i for i in out["items"] if i["label"] == "大模型 API Key")
    assert llm["status"] == "warn"
    assert "profile=qwen" in llm["detail"] and "DASHSCOPE_API_KEY" in llm["detail"]


# ---------- 源码锁：8 处硬闸不得回归 ----------

_PATCHED_FILES = [
    "test_agent/skills/builtin.py",        # gen_tests + log_analyze（2 处）
    "test_agent/graph/builder.py",         # 增补守卫
    "test_agent/graph/nodes.py",           # repair_strategy llm_fn
    "test_agent/skills/evaluate.py",       # map/gen_report/review_report（3 处）
    "test_agent/sandbox/preflight.py",     # 环境自检 LLM 项
]


def test_硬编码key闸不得回归():
    root = Path(__file__).resolve().parent.parent
    for rel in _PATCHED_FILES:
        src = (root / rel).read_text(encoding="utf-8")
        assert 'os.environ.get("DEEPSEEK_API_KEY")' not in src, rel
        assert "effective_key" in src, rel                 # 统一解析已接线
    # llm_client.py 兜底同样不得直读（走 llm_profile.current）
    src = (root / "test_agent/skills/llm_client.py").read_text(encoding="utf-8")
    assert 'DEEPSEEK_API_KEY' not in src


# ---------- B：空集归因（4 种原因文案锁） ----------

def test_空集归因_no_key():
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message("未配置 Key，跳过用例生成（诚实降级）："
                           "未配置当前模型的 API Key（profile=qwen，环境变量 DASHSCOPE_API_KEY）")
    assert "原因：未配置 Key" in m and "设置页" in m
    assert "不可解析" not in m                              # 不再硬编码猜测


def test_空集归因_解析失败():
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message("LLM 返回无法解析为用例（诚实降级，本轮不生成）；"
                           "原始输出已留档 llm_parse_fail/task1/")
    assert "无法解析" in m and "对话增补" in m


def test_空集归因_静态闸全剔():
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message("全部用例未过静态审查（诚实降级）")
    assert "静态审查" in m


def test_空集归因_模板全废():
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message("全部用例模板展开失败（槽位无效，诚实降级）")
    assert "模板展开失败" in m


def test_空集归因_未知原因兜底不编造():
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message(None)
    assert "原因未知" in m
    m2 = _empty_set_message("某种没见过的原因")
    assert "原因未知" in m2 and "某种没见过的原因" not in m2


def test_空集归因_文案与collect闸区分():
    # 容器收集闸废案是第 5 种 reason——不属于四类，落兜底（不误归因为解析失败）
    from test_agent.graph.builder import _empty_set_message
    m = _empty_set_message("生成物未过容器内 --collect-only 可收集性检查（整体废案）")
    assert "原因未知" in m and "无法解析" not in m
