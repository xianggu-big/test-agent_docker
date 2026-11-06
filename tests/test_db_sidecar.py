"""黑包阶段2·db sidecar 编排（docs/黑盒能力补齐任务包-20261106.md 测试矩阵）。

db_services 配置解析（缺字段必报错）+ 端口就绪等待超时分支（stub 永不就绪不挂死）。
"""
import pytest


class TestParseDbServices:
    def test_ok_minimal(self):
        from test_agent.skills.db_sidecar import parse_db_services
        spec = parse_db_services({"engine": "mariadb", "port": 3306,
                                  "env": {"MARIADB_DATABASE": "shop"}})
        assert spec["engine"] == "mariadb" and spec["port"] == 3306

    def test_missing_engine_raises(self):
        from test_agent.skills.db_sidecar import parse_db_services
        with pytest.raises(ValueError, match="engine"):
            parse_db_services({"port": 3306, "env": {}})

    def test_missing_port_raises(self):
        from test_agent.skills.db_sidecar import parse_db_services
        with pytest.raises(ValueError, match="port"):
            parse_db_services({"engine": "mariadb", "env": {}})

    def test_missing_env_raises(self):
        from test_agent.skills.db_sidecar import parse_db_services
        with pytest.raises(ValueError, match="env"):
            parse_db_services({"engine": "mariadb", "port": 3306})

    def test_bad_engine_raises(self):
        from test_agent.skills.db_sidecar import parse_db_services
        with pytest.raises(ValueError, match="engine"):
            parse_db_services({"engine": "oracle", "port": 1521, "env": {"X": "1"}})

    def test_migrate_files_optional_list(self):
        from test_agent.skills.db_sidecar import parse_db_services
        spec = parse_db_services({"engine": "postgres", "port": 5432,
                                  "env": {"POSTGRES_DB": "shop"},
                                  "migrate": ["sql/001_init.sql"]})
        assert spec["migrate"] == ["sql/001_init.sql"]


class TestWaitReady:
    def _stub_runner_never_ready(self):
        class _Res:
            exit_code = 1
            stdout = ""
            stderr = "not ready"
        class _R:
            def exec_in(self, container, cmd, **kw):
                return _Res()
        return _R()

    def test_wait_ready_times_out_cleanly(self):
        """永不就绪 → 到达超时即报错返回（不挂死、不重试烧钱）。"""
        from test_agent.skills.db_sidecar import wait_db_ready
        with pytest.raises(TimeoutError):
            wait_db_ready(self._stub_runner_never_ready(), container=object(),
                          probe_cmd=["mysqladmin", "ping"], timeout_s=1, interval_s=0.2)

    def test_wait_ready_passes_on_success(self):
        from test_agent.skills.db_sidecar import wait_db_ready
        class _Res:
            exit_code = 0
        class _R:
            def exec_in(self, container, cmd, **kw):
                return _Res()
        wait_db_ready(_R(), container=object(), probe_cmd=["mysqladmin", "ping"],
                      timeout_s=2, interval_s=0.1)     # 不抛即过


def test_recipe_rejects_bad_db_services():
    """recipe 带 db_services 段时走校验闸（缺字段必报错——测试矩阵：sidecar 配置解析）。"""
    from test_agent.skills.recipe import RecipeRejected, validate_recipe
    base = {"schema_version": "1", "language": "python", "runtime_version": "3.12",
            "base_image": "python:3.12-slim", "package_manager": "pip",
            "install": ["pip install httpx"], "test_framework": "pytest",
            "evidence": {"detected_from": ["requirements.txt"], "stack": ["python"]}}
    bad = dict(base, db_services={"port": 3306, "env": {"MARIADB_DATABASE": "shop"}})
    with pytest.raises(RecipeRejected, match="engine"):
        validate_recipe(bad)
    good = dict(base, db_services={"engine": "mariadb", "port": 3306,
                                   "env": {"MARIADB_DATABASE": "shop",
                                           "MARIADB_ROOT_PASSWORD": "x"}})
    out = validate_recipe(good)
    assert out["db_services"]["engine"] == "mariadb"
