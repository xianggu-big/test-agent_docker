# -*- coding: utf-8 -*-
"""快照路线（任务卡-D+ 路线二，golden_file oracle）单元测试。

口径锁：结构 diff 三态（新增/删除/变更，含字段路径）；基线更新必须显式确认
（防"改基线掩盖回归"）；无基线首跑只录不判；DOM 骨架确定性（同 HTML 两次
捕获同构）；旗标关=写入函数拒绝执行（只读 compare/load 不设闸，离线可锁口径）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.snapshot_route import (compare, compare_dom,
                                              capture_dom_structure,
                                              load_baseline, record_baseline,
                                              update_baseline)


@pytest.fixture()
def snap_env(tmp_path, monkeypatch):
    """隔离基线目录+开启路线旗标（写入函数的执行闸）。"""
    from test_agent import config
    monkeypatch.setattr(config, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(config, "SNAPSHOT_ROUTE_ENABLED", True)
    return config


_HTML = """<html><body>
  <div id="app" class="wrap">
    <h1 class="hero">兔兔记账本</h1>
    <button id="add" class="btn primary ok">添加</button>
    <ul class="list"><li>项目A</li><li>项目B</li></ul>
  </div>
</body></html>"""


# ---------- 基线存储与更新安全边界 ----------

def test_无基线_首跑只录不判(snap_env):
    assert load_baseline("proj", "first") is None          # 首跑：无基线=只录不判的前提
    record_baseline("proj", "first", {"a": 1})
    assert load_baseline("proj", "first") == {"a": 1}


def test_record_已存在_拒绝静默覆盖(snap_env):
    record_baseline("p", "s", {"v": 1})
    with pytest.raises(RuntimeError, match="已存在"):
        record_baseline("p", "s", {"v": 99})


def test_compare_三态_diff_含路径(snap_env):
    record_baseline("p", "s", {"list": [1, 2, 3], "old": "x", "n": {"deep": 1}})
    rep = compare(load_baseline("p", "s"), {"list": [1, 2], "n": {"deep": 2},
                                            "added": True})
    kinds = {d["kind"] for d in rep["diffs"]}
    assert kinds == {"added", "removed", "changed"}
    paths = {d["path"] for d in rep["diffs"]}
    assert "$.list[2]" in paths            # removed 元素带索引路径
    assert "$.old" in paths and "$.n.deep" in paths and "$.added" in paths
    assert rep["same"] is False


def test_compare_相同_零diff():
    payload = {"a": [1, {"b": "x"}], "c": 3}
    assert compare(payload, dict(payload))["same"] is True


def test_update_baseline_无确认拒绝(snap_env):
    record_baseline("p", "s", {"v": 1})
    with pytest.raises(RuntimeError, match="确认"):
        update_baseline("p", "s", {"v": 2})               # 无确认=拒绝（防掩盖回归）
    assert load_baseline("p", "s") == {"v": 1}            # 基线未被偷改
    update_baseline("p", "s", {"v": 2}, confirm=True)     # 显式确认=放行
    assert load_baseline("p", "s") == {"v": 2}


def test_旗标关_写入函数拒绝(tmp_path, monkeypatch):
    from test_agent import config
    monkeypatch.setattr(config, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(config, "SNAPSHOT_ROUTE_ENABLED", False)
    with pytest.raises(RuntimeError, match="MTA_SNAPSHOT_ROUTE"):
        record_baseline("p", "s", {"a": 1})
    with pytest.raises(RuntimeError, match="MTA_SNAPSHOT_ROUTE"):
        update_baseline("p", "s", {"a": 1}, confirm=True)


# ---------- DOM 骨架 ----------

def test_dom骨架_确定性_两次捕获同构():
    assert capture_dom_structure(_HTML) == capture_dom_structure(_HTML)


def test_dom骨架_抓到class变更路径():
    base = capture_dom_structure(_HTML)
    mutated = _HTML.replace('class="btn primary ok"', 'class="btn primary warn"')
    rep = compare_dom(base, capture_dom_structure(mutated))
    assert rep["same"] is False
    paths = [d["path"] for d in rep["diffs"]]
    assert any(p.endswith(".class") for p in paths), paths
    changed = next(d for d in rep["diffs"] if d["path"].endswith(".class"))
    assert "ok" in changed["baseline"] and "warn" in changed["current"]   # 定位到具体变更


def test_dom骨架_忽略空白噪声():
    base = capture_dom_structure(_HTML)
    spaced = _HTML.replace('<ul class="list">', '<ul class="list"   >')
    assert compare_dom(base, capture_dom_structure(spaced))["same"] is True


def test_dom骨架_删除元素_removed路径():
    base = capture_dom_structure(_HTML)
    gone = _HTML.replace("<li>项目B</li>", "")
    rep = compare_dom(base, capture_dom_structure(gone))
    assert not rep["same"] and rep["diffs"][0]["kind"] == "removed"
