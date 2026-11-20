"""包 1 能力层测试（2026-11-20 设计稿 §3.8；实施附录件 4 schema 实文）。

覆盖：菜单语言过滤/旗标联动/read 白名单与截窗/edit diff 四分类 AST 闸/
normalize 归一（metrics 白名单+证据降级）/动作指纹键排序。
铁律：零容器、零 LLM、零真图。"""
import json

import pytest

from test_agent.skills.capabilities import (CapResult, TaskCtx, action_fingerprint,
                                            classify_edit_diff, capability_menu,
                                            find_capability, normalize_result)


def _ctx(tmp_path, **snap):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return TaskCtx(task_id=1, session_id=1, workspace=ws, container_name="c",
                   primary_language=snap.pop("lang", "python"), image="mta-test:py312",
                   state_snapshot=snap, conn=None, runner=None,
                   artifacts_dir=tmp_path / "artifacts")


# ---------- 菜单 ----------

def test_menu_language_and_flag(tmp_path):
    """菜单按语言过滤（"*" 恒过）+ snapshot 旗标联动（关=不入菜单）。"""
    ctx = _ctx(tmp_path, lang="python")
    ids = [m["id"] for m in capability_menu(ctx)]
    assert "test.observe.read" in ids and "test.run.unit" in ids
    assert "test.snapshot" not in ids, "SNAPSHOT_ROUTE_ENABLED 默认关=snapshot 不入菜单"
    assert all({"id", "description", "risk"} <= set(m) for m in capability_menu(ctx))


def test_menu_snapshot_flag_on(tmp_path, monkeypatch):
    from test_agent import config
    monkeypatch.setattr(config, "SNAPSHOT_ROUTE_ENABLED", True)
    ctx = _ctx(tmp_path)
    assert "test.snapshot" in [m["id"] for m in capability_menu(ctx)]


def test_find_capability_language_gate(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, lang="python")
    assert find_capability(ctx, "test.run.unit") is not None
    assert find_capability(ctx, "test.nonexistent") is None


# ---------- read 白名单与截窗（0-D + 调研修正 2） ----------

def test_read_whitelist_denies_data_and_escape(tmp_path):
    from test_agent.skills.capabilities import _cap_observe_read
    ctx = _ctx(tmp_path)
    for bad in ("data/test_agent.db", "../../etc/passwd", "/etc/passwd", "config/x"):
        r = _cap_observe_read(ctx, {"path": bad, "mode": "read"})
        assert r.status == "denied", f"{bad} 必须拒绝"


def test_read_relative_path_ok_and_window(tmp_path):
    from test_agent.skills.capabilities import _cap_observe_read
    ctx = _ctx(tmp_path)
    f = ctx.workspace / "src" / "app.py"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join(f"line{i}" for i in range(1, 301)), encoding="utf-8")
    r = _cap_observe_read(ctx, {"path": "src/app.py", "mode": "read",
                                "offset": 250, "limit": 500})
    assert r.status == "ok"
    assert "共 300 行" in r.summary and "行 250-" in r.summary
    assert len(r.evidence) == 1 and r.evidence[0]["sha256"]
    assert r.evidence[0]["path"].startswith(str(ctx.artifacts_dir)), "证据必须落 artifacts 目录"


def test_grep_capped_at_50(tmp_path):
    from test_agent.skills.capabilities import _cap_observe_read
    ctx = _ctx(tmp_path)
    f = ctx.workspace / "big.txt"
    f.write_text("\n".join(f"hit {i} needle" for i in range(80)), encoding="utf-8")
    r = _cap_observe_read(ctx, {"path": "big.txt", "mode": "grep", "pattern": "needle"})
    assert "50 命中" in r.summary, "grep 必须 ≤50 条封顶"


# ---------- edit diff 四分类（§3.6；369 金样负例） ----------

BASE = ("import calc\n"
        "\n"
        "\n"
        "def test_a():\n"
        "    assert calc.f(1) == 1\n")


def test_edit_diff_layout_allowed():
    assert classify_edit_diff(BASE, BASE.replace("import calc", "import calc as c\nimport os")) == "layout"


def test_edit_diff_assertion_rejected():
    assert classify_edit_diff(BASE, BASE.replace("== 1", "== 2")) == "assertion"


def test_edit_diff_deletion_rejected():
    assert classify_edit_diff(BASE + "\n\ndef test_b():\n    assert 1\n", BASE) == "deletion"


def test_edit_diff_fixture_allowed():
    new = BASE.replace("def test_a():", "def test_a():\n    x = calc.ready()  # fixture wiring",
                       1)
    assert classify_edit_diff(BASE, new) == "fixture"


def test_edit_diff_syntax_error_conservative():
    assert classify_edit_diff(BASE, "def broken(:\n") == "assertion"


# ---------- normalize 归一（§6.2 铁律） ----------

def test_normalize_strips_non_parser_metrics():
    """metrics 键白名单：模型摘要/verdict 渗不进数字层。"""
    r = normalize_result(CapResult(status="ok", summary="s",
                                   metrics={"passed": 3, "verdict": "pass",
                                            "llm_said": "all good", "failed": "x"},
                                   evidence=[]))
    assert r.metrics == {"passed": 3}, "非白名单/非数值键全部剔除"


def test_normalize_missing_evidence_downgrades():
    r = normalize_result(CapResult(status="ok", summary="s", metrics={},
                                   evidence=[{"kind": "x"}]))  # 缺 path/sha256
    assert r.status == "unsupported", "无合规证据的 ok 必须降级"


def test_normalize_ok_kept_with_valid_evidence(tmp_path):
    p = tmp_path / "e.txt"
    import hashlib
    p.write_text("x", encoding="utf-8")
    r = normalize_result(CapResult(status="ok", summary="s", metrics={"passed": 1},
                                   evidence=[{"kind": "f", "path": str(p),
                                              "sha256": hashlib.sha256(b"x").hexdigest()}]))
    assert r.status == "ok" and len(r.summary) <= 200


# ---------- 动作指纹 ----------

def test_fingerprint_key_order_invariant():
    """args 键排序：同义重排不产生新指纹（防绕过重复闸）。"""
    a = action_fingerprint("test.run.unit", {"targets": ["t.py"], "timeout_s": 60})
    b = action_fingerprint("test.run.unit", {"timeout_s": 60, "targets": ["t.py"]})
    assert a == b
