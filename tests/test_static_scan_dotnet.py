"""白包阶段5·.NET Tier3 兜底（static_scan 对 .cs 的表现，docs/多语言工具链接入任务包-20261106.md）。

任务卡明文：仅确认 static_scan 对 .cs 文件的兜底表现，修复明显误报/漏报。
终验实锤：①.cs 原本零识别（枚举不含 .cs）②空 catch 跨行排版逐行匹配永漏报。
"""


def test_cross_line_empty_catch_detected(tmp_path):
    """跨行空 catch（C#/Java 常规排版）必须命中——逐行匹配漏报实锤修复。"""
    from test_agent.skills.static_scan import scan_workspace
    (tmp_path / "S.cs").write_text(
        "class S {\n"
        "    void M() {\n"
        "        try { Work(); }\n"
        "        catch (Exception ex)\n"
        "        {\n"
        "        }\n"
        "    }\n"
        "}\n", encoding="utf-8")
    res = scan_workspace(tmp_path)
    assert any("空 catch" in r for r in res["risk_lines"]), res["risk_lines"]


def test_single_line_empty_catch_still_detected(tmp_path):
    """单行空 catch 既有行为保持（跨行拆分不回归）。"""
    from test_agent.skills.static_scan import scan_workspace
    (tmp_path / "s.java").write_text("try { x(); } catch (Exception e) {}\n", encoding="utf-8")
    res = scan_workspace(tmp_path)
    assert any("空 catch" in r for r in res["risk_lines"])


def test_csharp_symbol_and_language(tmp_path):
    """.cs 纳入扫描：csharp 符号正则命中（public/private 修饰符形态）+ primary_language。"""
    from test_agent.skills.static_scan import scan_workspace
    (tmp_path / "S.cs").write_text(
        "namespace N {\n"
        "    public class S {\n"
        "        public int Add(int a, int b) { return a + b; }\n"
        "        private void Trim() { }\n"
        "    }\n"
        "}\n", encoding="utf-8")
    res = scan_workspace(tmp_path)
    assert res["files"] == 1 and res["primary_language"] == "csharp"
    assert res["symbols_total"] >= 2


def test_python_files_unaffected(tmp_path):
    """python 文件扫描行为零变化（.cs 扩展不回归既有）。"""
    from test_agent.skills.static_scan import scan_workspace
    (tmp_path / "m.py").write_text(
        "def ok():\n    pass\n\n\ndef risky():\n    try:\n        pass\n    except Exception:\n"
        "        pass\n", encoding="utf-8")
    res = scan_workspace(tmp_path)
    assert res["files"] == 1 and res["primary_language"] == "python"
    assert any("except 后直接 pass" in r for r in res["risk_lines"])
