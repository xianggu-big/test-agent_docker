# -*- coding: utf-8 -*-
"""任务6 java 半（2026-11-22）：多模块 jacoco 兜底接线——适配器渲染+旗标两态。

背景（存疑 35 姊妹首案/multi60 实锤）：多模块 Maven 每模块各产 jacoco.xml——
nodes java 分支只 cat 根 target/site/jacoco/jacoco.xml，多模块拿不到统一 TOTAL
→ 覆盖率解析失败熔断。兜底=find /work 全部 jacoco.xml → jacoco_multimodule.parse_reports
合并（scripts/plugins，CLI 形态与 D2 能力同源）→ 统一文本渲染。
旗标 JACOCO_MULTIMODULE_FALLBACK 默认 False=现状逐字节不变。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 两份最小 jacoco XML fixture（模块 A 全覆盖、模块 B 半覆盖）
XML_A = """<report name="mod-a">
 <counter type="INSTRUCTION" missed="0" covered="100"/>
 <counter type="LINE" missed="0" covered="50"/>
</report>"""
XML_B = """<report name="mod-b">
 <counter type="INSTRUCTION" missed="10" covered="30"/>
 <counter type="LINE" missed="5" covered="15"/>
</report>"""


def _render(texts):
    from test_agent.skills.java_adapter import render_multimodule_coverage
    return render_multimodule_coverage(texts)


def test_multimodule_render_merges_line_counters():
    out = _render([XML_A, XML_B])
    assert out is not None
    assert "multimodule" in out
    # LINE 合并：covered=50+15=65 missed=0+5=5 → pct=65/70≈93%（LINE 口径非 INSTRUCTION）
    assert "93" in out


def test_multimodule_render_single_module_still_works():
    out = _render([XML_A])
    assert out is not None and "mod" in out or "modules=1" in out


def test_multimodule_render_empty_honest_no_coverage():
    """空列表=诚实 no_coverage 降级（不熔断——jacoco_multimodule 契约）。"""
    out = _render([])
    assert out is not None and "no_coverage" in out


def test_multimodule_render_bad_xml_honest_degrade():
    """坏 XML=诚实降级段（不炸主链路）。"""
    out = _render(["<not-xml", XML_A])
    assert out is not None  # 降级但非 None
    assert "mod" in out or "E_BAD_XML" in out or "degraded" in out.lower()


def test_flag_off_by_default():
    import test_agent.config as config
    assert config.JACOCO_MULTIMODULE_FALLBACK is False, "旗标默认关（逐字节不变铁律）"


def test_parse_reports_direct_source_check():
    """与解析器真源对账：render 的合并口径=parse_reports 的 LINE 计数器。"""
    import json
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p1 = Path(td) / "a.xml"
        p2 = Path(td) / "b.xml"
        p1.write_text(XML_A, encoding="utf-8")
        p2.write_text(XML_B, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/plugins/jacoco_multimodule.py"),
             str(p1), str(p2)], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        assert d["status"] == "ok"
        # LINE: covered 65 / total 70
        assert d["metrics"]["covered"] == 65 and d["metrics"]["total"] == 70
