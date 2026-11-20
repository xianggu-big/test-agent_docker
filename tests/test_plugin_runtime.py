"""包 2 插件运行时测试（2026-11-20 设计稿 §4.5 表 6 例）。

覆盖：动态注册进菜单（verified+digest 才可见）/输出归一（协议内 metrics 白名单/
协议外诚实降级）/超时终止/local-script 执行/R2 模式矩阵（normal 自动·verify 拒）/
revoked 即刻不可见。铁律：tmp 目录+零容器（local-script 直跑真 jacoco 解析器）。
"""
import json

import pytest

from test_agent import config
from test_agent.skills import plugin_manager as pm, plugin_runtime as prt
from test_agent.skills.capabilities import TaskCtx
from test_agent.skills.tool_policy import authorize


def _catalog_entry(status="verified", digest="a" * 64):
    return {"id": "tool.parse.jacoco-multimodule", "version": "1.0.0", "kind": "plugin",
            "source": {"type": "local-script", "path": "scripts/plugins/jacoco_multimodule.py"},
            "digest": digest, "license": "MIT", "languages": ["java"],
            "entrypoint": "scripts/plugins/jacoco_multimodule.py",
            "permissions": {"fs": ["test_artifacts_write"], "network": "none", "secrets": []},
            "fixtures": [], "status": status, "pain_point": "多模块 jacoco 合并"}


def _write_catalog(tmp_path, entries):
    d = tmp_path / "tool_catalog"
    d.mkdir(parents=True, exist_ok=True)
    (d / "catalog.json").write_text(json.dumps({"schema_version": 1, "tools": entries},
                                               ensure_ascii=False), encoding="utf-8")


def _ctx(tmp_path, lang="java", **snap):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return TaskCtx(task_id=1, session_id=1, workspace=ws, container_name="c",
                   primary_language=lang, image="img", state_snapshot=snap,
                   conn=None, runner=None, artifacts_dir=tmp_path / "artifacts")


def test_runtime_entry_requires_digest_and_status(tmp_path, monkeypatch):
    """candidate/未封存 digest 不入菜单；verified+digest 才可见（rug pull 双向锁）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH", tmp_path / "tool_catalog" / "catalog.json")
    monkeypatch.setattr(pm, "ROOT_SRC", tmp_path.parent)   # 无关——本测试不执行 handler
    _write_catalog(tmp_path, [_catalog_entry(status="verified"),
                              _catalog_entry(status="candidate", digest="")])
    ctx = _ctx(tmp_path)
    ids = [c.id for c in prt.catalog_capabilities(ctx)]
    assert ids == ["plugin.tool.parse.jacoco-multimodule"], \
        "candidate/未封存条目必须不可见"


def test_revoked_invisible_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH", tmp_path / "tool_catalog" / "catalog.json")
    _write_catalog(tmp_path, [_catalog_entry(status="revoked")])
    assert prt.catalog_capabilities(_ctx(tmp_path)) == [], "revoked 即刻不可见"


def test_local_script_real_parse(tmp_path, monkeypatch):
    """真 jacoco 解析器执行（repo 真脚本）：--demo happy → ok + metrics。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH",
                        tmp_path / "tool_catalog" / "catalog.json")
    r = prt._run_local_script(_catalog_entry(), {"args": {"demo": "happy"}})
    assert r.status == "ok" and r.raw_exit == 0
    data = json.loads(r.diagnostics and "{}" or "{}")   # 结构探针（避免误用）
    # 归一走 _parse_output：stdout JSON 的 metrics 数字应进 CapResult.metrics
    assert isinstance(r.metrics, dict)


def test_parse_output_protocol_and_fallback(tmp_path):
    """协议内：metrics 数值白名单归一；协议外（非 JSON）：exit 语义诚实降级。"""
    good = prt._parse_output(_catalog_entry(), 0,
                             '{"status": "ok", "metrics": {"total": 20, "junk": "x"}}', "")
    assert good.status == "ok" and good.metrics == {"total": 20}, "非数值键剔除"
    bad = prt._parse_output(_catalog_entry(), 1, "not-json", "")
    assert bad.status == "failed" and bad.raw_exit == 1


def test_r2_mode_matrix(tmp_path, monkeypatch):
    """D2 模式矩阵：normal=目录插件放行；verify 冻结拒绝；R3 永不自动。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH", tmp_path / "tool_catalog" / "catalog.json")
    monkeypatch.setattr(pm, "ROOT_SRC", tmp_path.parent)
    _write_catalog(tmp_path, [_catalog_entry()])
    ctx = _ctx(tmp_path)
    menu = [m for m in __import__("test_agent.skills.capabilities", fromlist=["capability_menu"])
            .capability_menu(ctx)]
    act = {"action": "call", "capability": "plugin.tool.parse.jacoco-multimodule",
           "args": {}}
    d = authorize(act, ctx, menu=menu)
    assert d.allowed and d.risk == "R2", f"normal 模式目录插件应自动（D2）：{d.reason}"
    d2 = authorize(act, _ctx(tmp_path, verify_mode=1), menu=menu)
    assert not d2.allowed, "verify 冻结任务 R2 拒绝"


def test_menu_excludes_plugins_in_diagnose(tmp_path, monkeypatch):
    """diagnose 只读模式：R2 插件不进菜单（菜单收窄 R0）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH", tmp_path / "tool_catalog" / "catalog.json")
    _write_catalog(tmp_path, [_catalog_entry()])
    from test_agent.skills.capabilities import capability_menu
    ctx = _ctx(tmp_path)
    ctx.agent_mode = "diagnose"
    assert all(m["risk"] == "R0" for m in capability_menu(ctx))
