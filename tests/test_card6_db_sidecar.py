"""卡 6 测试锁：服务端库 sidecar 自动拉起接线（2026-11-15）。

检测✓+sidecar✓中间接线✗（1138 的 5432 connection refused 直接解释）——补：
①旗标 DB_SIDECAR_ENABLED（MTA_DB_SIDECAR 默认关=旧行为逐字节不变）
②确定性默认 spec + evidence DSN 凭据尽力提取（凭据不符=auth fail）
③同 pod 网络栈（network_mode=container:<被测容器>——被测容器 127.0.0.1 直通
  sidecar，DSN 的 localhost 无需改写，652 双容器方案的通用化）
④回收闭环（builder finally 清 mta-dbsidecar-<tid> + 启动清扫加前缀）。
"""
import json

import pytest

from test_agent import config
from test_agent.skills.db_probe import (_DEFAULT_SIDECAR_SPECS, _sidecar_spec_for,
                                         prepare_database)


# ---------- ① spec 构建与凭据提取 ----------

def test_sidecar_spec_defaults():
    spec = _sidecar_spec_for("pg", "no dsn here")
    assert spec["engine"] == "postgres" and spec["dsn_source"] == "default"
    assert spec["env"]["POSTGRES_PASSWORD"] == "mta_sidecar"


def test_sidecar_spec_dsn_credentials():
    """652 真实凭据形态：postgresql://album_user:secret123@localhost:5432/album_db"""
    spec = _sidecar_spec_for("pg", "db.py: postgresql://album_user:secret123@"
                                    "localhost:5432/album_db")
    assert spec["dsn_source"] == "evidence"
    assert spec["env"]["POSTGRES_USER"] == "album_user"
    assert spec["env"]["POSTGRES_PASSWORD"] == "secret123"
    assert spec["env"]["POSTGRES_DB"] == "album_db"


def test_sidecar_spec_mysql():
    spec = _sidecar_spec_for("mysql", "conf: mysql://app:pw@127.0.0.1:3306/shop_db")
    assert spec["engine"] == "mariadb"
    assert spec["env"]["MARIADB_USER"] == "app" and spec["env"]["MARIADB_DATABASE"] == "shop_db"


# ---------- ② 旗标两态 ----------

def test_flag_default_off():
    assert config.DB_SIDECAR_ENABLED is False, "默认关=旧路径逐字节不变"


def test_prepare_database_skip_when_flag_off(tmp_path, monkeypatch):
    """旗标关 → server 家族如实降级 skip（旧口径），绝不碰 docker。"""
    monkeypatch.setattr(config, "DB_SIDECAR_ENABLED", False)
    det = {"family": "pg", "evidence": ["pg://x"], "scripts": [], "sql_files": []}

    class _Boom:
        def exec_in(self, *a, **kw):
            raise AssertionError("旗标关不得触碰容器")

    out = prepare_database(_Boom(), object(), det, tmp_path, "python")
    assert out["status"] == "skip" and "MTA_DB_SIDECAR" in out["note"]


def test_prepare_database_sidecar_happy_path(tmp_path, monkeypatch):
    """旗标开 → spec 构建→同网络栈拉起→迁移→ok（runner 全 fake，零真 docker）。"""
    monkeypatch.setattr(config, "DB_SIDECAR_ENABLED", True)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE a(id int);",
                                                          encoding="utf-8")
    det = {"family": "pg", "evidence": ["db.py: postgresql://u1:p1@localhost:5432/db1"],
           "scripts": [], "sql_files": ["migrations/001_init.sql"]}

    class _Cont:
        name = "mta-run-42"

    class _Res:
        def __init__(self, exit_code=0, stdout=""):
            self.exit_code, self.stdout, self.stderr = exit_code, stdout, ""
            self.ok = exit_code == 0

    class _Runner:
        def __init__(self):
            self.started, self.mig_cmds = [], []

        @property
        def client(self):
            return self

        def containers(self):
            pass

        def containers_get(self, name):
            raise Exception("not found")

        def exec_in(self, c, cmd, workdir="/work", timeout_s=30):
            self.mig_cmds.append(" ".join(cmd)[:120])
            return _Res(0, "CREATE TABLE")

    r = _Runner()

    started = {}

    def _fake_ensure(runner, name, spec, network_name=None, network_mode=None):
        started["name"], started["mode"], started["spec"] = name, network_mode, spec
        return object()

    # prepare_database 内部 from .db_sidecar import ensure_db_sidecar（延迟）——patch 源头
    monkeypatch.setattr("test_agent.skills.db_sidecar.ensure_db_sidecar", _fake_ensure)

    out = prepare_database(r, _Cont(), det, tmp_path, "python")
    assert out["status"] == "ok", out["note"]
    assert started["name"] == "mta-dbsidecar-42"
    assert started["mode"] == "container:mta-run-42", "必须同 pod 网络栈（DSN 无需改写）"
    assert started["spec"]["env"]["POSTGRES_PASSWORD"] == "p1", "evidence 凭据优先"


def test_prepare_database_sidecar_fail_open(tmp_path, monkeypatch):
    """sidecar 拉起失败（镜像缺/超时）→ skip 降级留痕，不炸 provision。"""
    monkeypatch.setattr(config, "DB_SIDECAR_ENABLED", True)
    det = {"family": "pg", "evidence": [], "scripts": [], "sql_files": []}

    class _Cont:
        name = "mta-run-7"

    def _boom(*a, **kw):
        raise RuntimeError("pull access denied")

    monkeypatch.setattr("test_agent.skills.db_sidecar.ensure_db_sidecar", _boom)
    out = prepare_database(object(), _Cont(), det, tmp_path, "python")
    assert out["status"] == "skip" and "拉起失败" in out["note"]


# ---------- ③ 回收闭环 ----------

def test_sweep_prefixes_include_dbsidecar():
    import inspect
    from test_agent.api import server as srv
    src = inspect.getsource(srv._sweep_orphan_containers)
    assert "mta-dbsidecar-" in src, "启动清扫必须覆盖 sidecar 残留"


def test_builder_finally_cleans_sidecar():
    import inspect
    from test_agent.graph import builder as b
    src = inspect.getsource(b.run_task) + inspect.getsource(b.run_task_exec)
    assert src.count("mta-dbsidecar-{task_id}") == 2, "两个入口都要回收 sidecar"
