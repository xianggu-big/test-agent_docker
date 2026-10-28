"""W4 圈复杂度热点（skills/complexity.py）与 plan 提示词注入的机制单测。"""
import textwrap

from test_agent.skills.complexity import complexity_hotspots
from test_agent.skills.prompt_library import build_plan_prompt

COMPLEX_SRC = textwrap.dedent("""\
    def simple(x):
        return x + 1


    def tangled(items, flag, mode):
        if flag:
            for it in items:
                if it > 0 and mode == "a":
                    continue
                elif it < 0 and mode == "b":
                    break
                else:
                    while it:
                        it -= 1
                        if it % 2:
                            if it > 3 and mode != "c":
                                pass
                            elif it == 2 or flag == mode:
                                pass
        return items
""")


def test_hotspots_detects_complex_function(tmp_path):
    (tmp_path / "tangled.py").write_text(COMPLEX_SRC, encoding="utf-8")
    (tmp_path / "test_skip.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    hot = complexity_hotspots(tmp_path)

    assert hot, "复杂函数应被检出"
    assert all(h["file"] != "test_skip.py" for h in hot)   # 测试文件不计
    top = hot[0]
    assert top["name"] == "tangled"
    assert top["complexity"] >= 10 and top["rank"] in ("C", "D", "E", "F")
    assert "simple" not in {h["name"] for h in hot}        # 简单函数不上榜


def test_hotspots_empty_for_clean_workspace(tmp_path):
    (tmp_path / "ok.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    assert complexity_hotspots(tmp_path) == []


def test_plan_prompt_contains_hotspots(tmp_path):
    hot = [{"file": "tangled.py", "name": "tangled", "complexity": 12, "rank": "C", "lineno": 5}]
    on = build_plan_prompt("需求", hotspots=hot)
    off = build_plan_prompt("需求")
    assert "圈复杂度热点" in on and "tangled" in on and "语句分支覆盖" in on
    assert "圈复杂度热点" not in off
