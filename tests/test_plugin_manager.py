"""包 2 插件管理器测试（2026-11-20 设计稿 §4.5 表 8 例；D2 拍板=目录内固定版本自动安装）。

覆盖：摘要不符拒/未知 id 拒/local-script 封存与比对/安装登记/验证三 fixture/
提案只申请不安装/审批权限语义/回滚台账/目录准入校验（latest/缺字段/非法 status）。
铁律：tmp 库+tmp 目录+假 runner，零真容器（真机七步走通独立脚本）。
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from test_agent import config
from test_agent.skills import plugin_manager as pm

CATALOG = {
    "schema_version": 1,
    "tools": [
        {"id": "tool.parse.jacoco-multimodule", "version": "1.0.0", "kind": "plugin",
         "source": {"type": "local-script", "path": "scripts/plugins/jacoco_multimodule.py"},
         "digest": "", "license": "MIT", "languages": ["java"],
         "entrypoint": "scripts/plugins/jacoco_multimodule.py",
         "permissions": {"fs": ["test_artifacts_write"], "network": "none", "secrets": []},
         "fixtures": [{"name": "happy_path", "cmd": ["python", "{entrypoint}", "--demo", "happy"],
                       "expect_exit": 0, "expect_contains": "total"}],
         "status": "candidate"},
        {"id": "tool.demo.py", "version": "1.2.0", "kind": "plugin",
         "source": {"type": "pypi", "pkg": "demo-pkg", "url": "https://pypi.org/project/demo-pkg/"},
         "digest": "", "license": "MIT", "languages": ["python"],
         "entrypoint": "demo-pkg run",
         "permissions": {"fs": [], "network": "none", "secrets": []},
         "fixtures": [], "status": "verified"},
    ],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "CATALOG_PATH", tmp_path / "tool_catalog" / "catalog.json")
    monkeypatch.setattr(pm, "CACHE_DIR", tmp_path / "tool_cache")
    monkeypatch.setattr(pm, "ROOT_SRC", tmp_path)
    cat_dir = tmp_path / "tool_catalog"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalog.json").write_text(json.dumps(CATALOG, ensure_ascii=False),
                                          encoding="utf-8")
    # 真 jacoco 解析器复制进 tmp 结构（fetch/verify 走真脚本——不是假夹具）
    real = ROOT / "scripts" / "plugins" / "jacoco_multimodule.py"
    plug_dir = tmp_path / "scripts" / "plugins"
    plug_dir.mkdir(parents=True)
    (plug_dir / "jacoco_multimodule.py").write_text(real.read_text(encoding="utf-8"),
                                                    encoding="utf-8")
    from test_agent.store import db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.get_conn()
    return conn, tmp_path


def _fake_runner(container):
    """假 runner：copy_dir_into 记录；exec_in 返回预设码。"""
    class _R:
        def copy_dir_into(self, c, host_dir, keep_git=False):
            self.copied = str(host_dir)

        def exec_in(self, c, cmd, timeout_s=60, **kw):
            self.last_cmd = list(cmd)
            return self._next

    r = _R()
    r._next = type("E", (), {"exit_code": 0, "stdout": "ok", "stderr": ""})()
    return r


def test_catalog_validation_rejects_latest(env):
    """目录准入：version=latest 拒绝（设计稿 §7.1——不接受 latest）。"""
    conn, tmp = env
    bad = json.loads(json.dumps(CATALOG))
    bad["tools"][0]["version"] = "latest"
    (tmp / "tool_catalog" / "catalog.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(pm.CatalogError):
        pm.load_catalog()


def test_find_entry_by_id_and_version(env):
    conn, tmp = env
    assert pm.find_entry("tool.demo.py")["version"] == "1.2.0"
    assert pm.find_entry("tool.demo.py", version="9.9.9") is None
    assert pm.find_entry("tool.nothere") is None


def test_fetch_local_script_seals_digest(env):
    """local-script：首次 fetch 封存新摘要（sealed=False 提示人工写回）；
    人工写回后再次 fetch → digest 比对一致。"""
    conn, tmp = env
    entry = pm.find_entry("tool.parse.jacoco-multimodule")
    r1 = pm.fetch_artifact(entry)
    assert r1["ok"] and not r1["sealed"], "首次=seal 新摘要"
    assert r1["digest"], "必须产出 sha256"
    entry2 = dict(entry, digest=r1["digest"])
    r2 = pm.fetch_artifact(entry2)
    assert r2["ok"] and r2["sealed"] and "一致" in r2["detail"]


def test_fetch_digest_mismatch_rejected(env):
    """摘要不符拒绝（rug pull 结构性防御：内容一变摘要就变=拒绝+告警）。"""
    conn, tmp = env
    entry = dict(pm.find_entry("tool.parse.jacoco-multimodule"), digest="0" * 64)
    r = pm.fetch_artifact(entry)
    assert not r["ok"] and "digest 不符" in r["detail"]


def test_install_local_script_registers(env):
    """local-script 型：无容器安装，tool_installations 登记 installed。"""
    conn, tmp = env
    entry = pm.find_entry("tool.parse.jacoco-multimodule")
    r = pm.install_in_task(conn, entry, runner=None, container=None, task_id=7)
    assert r["ok"]
    row = conn.execute("SELECT status, catalog_id FROM tool_installations "
                       "WHERE task_id=7").fetchone()
    assert row["status"] == "installed" and row["catalog_id"] == entry["id"]


def test_install_failure_registered(env):
    """容器型缓存为空 → failed 如实登记（不炸主链路）。"""
    conn, tmp = env
    entry = pm.find_entry("tool.demo.py")
    r = pm.install_in_task(conn, entry, runner=None, container=None, task_id=8)
    assert not r["ok"]
    assert conn.execute("SELECT status FROM tool_installations WHERE task_id=8"
                        ).fetchone()["status"] == "failed"


def test_verify_fixtures_and_gate(env):
    """契约验证：fixture 全过=verified 台账；缺 fixtures 条目=拒验（只跑正常路径不算过）。"""
    conn, tmp = env
    entry = pm.find_entry("tool.parse.jacoco-multimodule")
    r = pm.verify_entry(conn, entry, runner=None, container=None)
    assert r["ok"], f"happy fixture 必须过（demo 合成报告）：{r['results']}"
    row = conn.execute("SELECT status FROM tool_installations WHERE catalog_id=? "
                       "AND task_id=0", (entry["id"],)).fetchone()
    assert row["status"] == "verified"
    no_fx = pm.find_entry("tool.demo.py")
    r2 = pm.verify_entry(conn, no_fx, runner=None, container=None)
    assert not r2["ok"] and "未声明 fixtures" in r2["detail"]


def test_proposal_never_self_approves(env):
    """提案：模型只申请（pending）；审批必须人工（decide 语义=一次性 pending 更新）。"""
    conn, tmp = env
    pid = pm.propose_capability(conn, 7, "需要 GraphQL 契约工具",
                                "菜单无对应能力，证据=...".ljust(10))
    row = conn.execute("SELECT status FROM plugin_proposals WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "pending"
    r = pm.decide_proposal(conn, pid, approve=True, by="admin:human")
    assert r["ok"]
    r2 = pm.decide_proposal(conn, pid, approve=False, by="agent:x")   # 已审批=无 pending 行
    assert not r2["ok"], "二次审批不可能（幂等防 Agent 自批改票）"


def test_set_status_and_rollback(env):
    """目录状态机（人工）：candidate→verified；任务级回滚台账。"""
    conn, tmp = env
    r = pm.set_status(conn, "tool.demo.py", "active")
    assert r["ok"]
    assert pm.find_entry("tool.demo.py")["status"] == "active"
    conn.execute("INSERT INTO tool_installations(task_id, catalog_id, version, status) "
                 "VALUES(9,'tool.demo.py','1.2.0','installed')")
    conn.commit()
    n = pm.rollback_task_installations(conn, 9)
    assert n == 1
    assert conn.execute("SELECT status FROM tool_installations WHERE task_id=9"
                        ).fetchone()["status"] == "rolled_back"

def test_fetch_unsupported_channel_rejected(env):
    """php-ext/go-install 通道未实现 → 显式拒绝（防 pip 错装同名包——pypi 的
    'pcov' 与 pecl 的 pcov 毫无关系）。"""
    conn, tmp = env
    entry = dict(pm.find_entry("tool.demo.py"),
                 source={"type": "php-ext", "pkg": "pcov"})
    r = pm.fetch_artifact(entry)
    assert not r["ok"] and "未实现" in r["detail"]
