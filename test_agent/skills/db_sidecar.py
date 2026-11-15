"""DB sidecar 编排（黑盒能力补齐阶段2，docs/黑盒能力补齐任务包-20261106.md）。

Testcontainers 模式抄写（自实现，调研结论见任务卡）：容器生命周期/wait strategy/
docker network 互通/清理——对应 runner.ensure_container+cleanup、wait_db_ready、
docker SDK networks、runner.cleanup 惯例。

设计纪律：
- sidecar 端口**不对宿主暴露**（仅 docker network 内部互通——黑盒 DB 验证不需要宿主连接）；
- recipe 的 `db_services` 声明段缺字段必报错（ValueError，面向用户可读）；
- 探活=sidecar 容器内 exec（mysqladmin ping / pg_isready），超时即报错不挂死。
"""
import logging

from .. import config

log = logging.getLogger(__name__)

_ENGINES = {
    # mariadb:11 无 mysqladmin（容器实测 2026-11-07，只有 mariadb-admin）
    "mariadb": {"image": "mariadb:11",
                "probe": "mariadb-admin ping -h 127.0.0.1 --silent"},
    "postgres": {"image": "postgres:16-alpine", "probe": "pg_isready -h 127.0.0.1"},
}


def parse_db_services(spec: dict) -> dict:
    """recipe.db_services 段解析与校验；缺字段/非法值抛 ValueError（消息面向用户）。"""
    if not isinstance(spec, dict):
        raise ValueError("db_services 必须是对象")
    missing = [f for f in ("engine", "port", "env") if not spec.get(f)]
    if missing:
        raise ValueError(f"db_services 缺字段：{missing}")
    engine = str(spec["engine"]).lower()
    if engine not in _ENGINES:
        raise ValueError(f"db_services.engine={engine!r} 不在支持列表 {sorted(_ENGINES)}")
    try:
        port = int(spec["port"])
    except (TypeError, ValueError):
        raise ValueError(f"db_services.port={spec['port']!r} 不是整数")
    env = spec["env"]
    if not isinstance(env, dict):
        raise ValueError("db_services.env 必须是对象")
    migrate = [str(m) for m in (spec.get("migrate") or [])]
    return {"engine": engine, "port": port, "env": dict(env), "migrate": migrate}


def wait_db_ready(runner, container, probe_cmd: list[str], timeout_s: float = 60.0,
                  interval_s: float = 1.0) -> None:
    """容器内探活直到就绪；超时抛 TimeoutError（不挂死、不无限重试）。

    Testcontainers wait strategy 同形态：轮询 interval_s，deadline 到即失败。"""
    import time
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        # workdir="/"：exec_in 默认 /work 是 mta-test 镜像约定，DB sidecar 镜像无该目录
        # （chdir 失败 exit 127，真机实锤 2026-11-07）
        r = runner.exec_in(container, probe_cmd, timeout_s=10, workdir="/")
        if r.exit_code == 0:
            return
        last = f"exit={r.exit_code} out={r.stdout[:120]!r} err={r.stderr[:120]!r}"
        time.sleep(interval_s)
    raise TimeoutError(f"DB sidecar 探活超时（{timeout_s}s）：{probe_cmd} 最后一次: {last}")


def ensure_db_sidecar(runner, name: str, spec: dict, network_name: str | None = None,
                      network_mode: str | None = None):
    """起 DB sidecar 容器并等待就绪；返回 docker 容器对象。

    - 环境变量来自 spec.env（MARIADB_ROOT_PASSWORD/POSTGRES_PASSWORD 等由 recipe 声明）；
    - network_name 给定时加入该网络（被测容器经容器名互通）；network_mode 给定时
      以该模式运行（2026-11-15 卡 6 接线：network_mode="container:<被测容器>"=
      testcontainers 同 pod 语义——sidecar 与被测容器共享网络栈，被测容器内
      127.0.0.1:port 直通 sidecar，项目 DSN 的 localhost 无需改写）；
    - 两者都缺=不接网络；互斥（network_mode 优先，同给时以 network_mode 为准）；
    - 端口不映射宿主（安全默认：黑盒验证不需要宿主连接）。
    """
    import docker as docker_sdk
    parsed = parse_db_services(spec)
    client = runner.client if hasattr(runner, "client") else docker_sdk.from_env()
    kwargs = dict(image=_ENGINES[parsed["engine"]]["image"], name=name,
                  environment=parsed["env"], detach=True)
    if network_mode:
        kwargs["network_mode"] = network_mode
    elif network_name:
        kwargs["network"] = network_name
    existing = None
    try:
        existing = client.containers.get(name)
        if existing.status != "running":
            existing.start()
    except Exception:  # noqa: BLE001 —— 不存在则新建
        existing = client.containers.run(**kwargs)
    wait_db_ready(runner, existing, ["sh", "-c", _ENGINES[parsed["engine"]]["probe"]],
                  timeout_s=90, interval_s=2.0)
    existing.reload()      # run()/get() 返回的 attrs.status 是快照（'created'），探活过后重取
    return existing


def run_migrations(runner, sidecar, spec: dict, sql_files: list[tuple[str, str]]) -> list[str]:
    """执行迁移/种子 SQL（文件名+内容二元组列表）；返回逐文件执行结果摘要。

    传输与 db_assert 同法：文件写入 sidecar /tmp 后经 CLI 导入（mariadb < / psql <）。"""
    parsed = parse_db_services(spec)
    engine, database = parsed["engine"], parsed["env"].get("MARIADB_DATABASE")
    if engine == "postgres":
        database = parsed["env"].get("POSTGRES_DB")
    out: list[str] = []
    for fname, content in sql_files:
        # 子路径压平（2026-11-15 卡 6 真机实锤：det.sql_files 常带 migrations/ 前缀，
        # /tmp/.mig_migrations/ 目录不存在 → cat can't open → 迁移失败）
        safe = fname.replace("/", "_").replace("\\", "_")
        runner.exec_in(sidecar, ["sh", "-c",
                                 f"cat > /tmp/.mig_{safe} << 'MIGEOF'\n{content}\nMIGEOF"],
                       timeout_s=15, workdir="/")
        if engine == "mariadb":
            cmd = ["sh", "-c",
                   f"mariadb -h 127.0.0.1 -uroot -p\"$MARIADB_ROOT_PASSWORD\" "
                   f"{database or ''} < /tmp/.mig_{safe} 2>&1"]
        else:
            cmd = ["sh", "-c",
                   f"psql -h 127.0.0.1 -U \"$POSTGRES_USER\" -d {database} "
                   f"< /tmp/.mig_{safe} 2>&1"]
        r = runner.exec_in(sidecar, cmd, timeout_s=120, workdir="/")
        out.append(f"{fname} exit={r.exit_code} {(r.stdout or r.stderr)[-300:]}")
    return out
