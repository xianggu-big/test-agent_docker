# -*- coding: utf-8 -*-
"""UI 交互幂等路线（期 A2）：确定性候选提取+oracle 渲染+执行结果 schema 对齐。

oracle=状态不变量（首次交互后计数为合法基线，双击/三连击不得再改变）——不依赖
LLM 判定；失败归类沿用 attribution 三分类（页面行为=product_defect），不新造 taxonomy。
"""
import json

from test_agent.skills.ui_idempotency import (extract_idem_candidates,
                                              render_idem_script, run_idempotency)


def _mk_page(tmp_path, name="index.html", body=None):
    body = body or (
        "<html><body><ul id='list'></ul>"
        "<button id='add' onclick=\"addItem()\">添加</button>"
        "<script>function addItem(){"
        "var li=document.createElement('li');li.textContent='x';"
        "document.getElementById('list').appendChild(li);}</script></body></html>")
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------- 候选提取（确定性，零 LLM） ----------

def test_提取带id按钮与计数容器(tmp_path):
    _mk_page(tmp_path)
    cases = extract_idem_candidates(tmp_path)
    assert len(cases) == 1
    c = cases[0]
    assert c["selector"]["spec"] == ["id", "add"]
    assert c["count_selector"] == "list" and c["count_kind"] == "rows"
    assert c["name"].startswith("test_交互幂等_")


def test_无id按钮不提取_v1定位稳定性(tmp_path):
    _mk_page(tmp_path, body="<button onclick='x()'>无id按钮</button>")
    assert extract_idem_candidates(tmp_path) == []


def test_上限截断(tmp_path):
    body = "".join(f"<button id='b{i}'>b{i}</button>" for i in range(6))
    _mk_page(tmp_path, body=body)
    assert len(extract_idem_candidates(tmp_path, max_cases=3)) == 3


# ---------- oracle 渲染（不变量语义进脚本） ----------

def test_渲染脚本含三段不变量断言(tmp_path):
    _mk_page(tmp_path)
    cases = extract_idem_candidates(tmp_path)
    script = render_idem_script(cases, "file:///work", "/tmp/e", "/tmp/j.xml")
    assert "after1 == base" in script                       # 首击生效基线
    assert "after2 == after1" in script                     # 双击不变量
    assert "after3 == after1" in script                     # 三连击不变量
    assert "重复提交缺陷" in script
    assert "dblclick" in script and "click_count=3" in script
    assert "file:///work" in script                         # 静态页 file:// 打开


# ---------- 执行结果 schema（对齐 evaluate/attribution 契约） ----------

def test_无候选如实降级(tmp_path):
    _mk_page(tmp_path, body="<p>没有交互元素</p>")
    from test_agent.skills import ui_idempotency as m
    class _R:                                                # 不应被触达
        def __getattr__(self, n): raise AssertionError("无候选不应起容器")
    out = json.loads(m.run_idempotency("ollama", {"workspace": str(tmp_path)}, 1, _R()))
    assert out["generated"] == 0 and "候选" in out["reason"]
