# -*- coding: utf-8 -*-
"""C8（2026-11-27 总包）：隔离纪律段注入红绿测试——旗标关=逐字节不变。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_flag_off_identity(monkeypatch):
    from test_agent.skills.prompt_library import SYSTEM_PROMPT, system_prompt_with_isolation
    from test_agent import config
    monkeypatch.setattr(config, "GEN_ISOLATION_ENABLED", False)
    assert system_prompt_with_isolation(SYSTEM_PROMPT) == SYSTEM_PROMPT


def test_flag_on_appends_discipline(monkeypatch):
    from test_agent.skills.prompt_library import (SYSTEM_PROMPT,
                                                  system_prompt_with_isolation)
    from test_agent import config
    monkeypatch.setattr(config, "GEN_ISOLATION_ENABLED", True)
    s = system_prompt_with_isolation(SYSTEM_PROMPT)
    assert s.startswith(SYSTEM_PROMPT)
    assert "用例隔离纪律" in s and "自建全部前置数据" in s


def test_gen_tests_call_sites_use_helper():
    """三处 SYSTEM_PROMPT 调用必须走助手（旗标开关才生效）。"""
    import pathlib
    s = (Path(__file__).resolve().parents[1] / "test_agent" / "skills" /
         "gen_tests.py").read_text(encoding="utf-8")
    assert s.count('llm.invoke([("system", system_prompt_with_isolation(SYSTEM_PROMPT))') == 3
    assert 'llm.invoke([("system", SYSTEM_PROMPT)' not in s
