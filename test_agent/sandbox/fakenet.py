# -*- coding: utf-8 -*-
"""任务2（2026-11-22 零 API 工作包）：伪造网络·桩服务器生命周期。

容器"有网的感觉、零真实外发"：WireMock 桩容器挂 --internal 网（mta-fakenet），
SUT 容器双网络（bridge 保 host.docker.internal MySQL + mta-fakenet 挂桩），
SUT 经环境变量 API_BASE_URL=http://mta-stub-{task_id}:8080 指向桩——internal 网
DNS 按容器名解析（交接文档机制段）。

- 桩容器唯一名 mta-stub-{task_id}（bench3 串账事故的容器名纪律）
- mappings：data/fakenet/{profile}/mappings/*.json bind mount 进桩
  （WireMock 原生格式：端点→响应/延迟/故障，fixedDelayMilliseconds/
  fault: CONNECTION_RESET_BY_PEER 原生故障注入）
- 幂等：同名桩已 running 复用；任务结束 cleanup_stub（网络不删=共享资源）
- fail-open：桩起失败返回 None，主任务照常（不引入新失败面——recipe 先例）
"""
import logging
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

STUB_IMAGE = "wiremock/wiremock:3x"
STUB_PORT = 8080
MAPPINGS_ROOT = Path("data/fakenet")   # 相对 cwd（平台进程根=data_agent 仓库根）


def stub_name(task_id) -> str:
    return f"mta-stub-{task_id}"


def stub_host(task_id) -> str:
    """internal 网内 SUT 侧访问桩的主机名（DNS 按容器名解析）。"""
    return f"{stub_name(task_id)}:{STUB_PORT}"


def ensure_stub(client, task_id, profile: str = "default") -> str | None:
    """起/复用桩容器（旗标与网络存在性双重守卫）。返回桩主机名；失败 None（fail-open）。

    前置：mta-fakenet 网已存在（scripts/ensure_fake_net.py 或 provision 侧先建）。
    """
    if not config.FAKE_NET_ENABLED:
        return None
    name = stub_name(task_id)
    mdir = MAPPINGS_ROOT / profile / "mappings"
    if not mdir.exists() or not any(mdir.glob("*.json")):
        log.warning("[fakenet] 无桩定义 %s——不起桩（fail-open）", mdir)
        return None
    try:
        c = client.containers.get(name)
        if c.status != "running":
            c.start()
        return stub_host(task_id)
    except Exception:                     # noqa: BLE001 —— NotFound → 新建
        pass
    try:
        client.containers.run(
            STUB_IMAGE,
            name=name,
            command=["--verbose"],
            detach=True,
            network=config.FAKE_NET_NAME,
            volumes={str(mdir.resolve()): {"bind": "/home/wiremock/mappings",
                                           "mode": "ro"}},
            labels={"mta": "fakenet-stub", "task": str(task_id)},
        )
        log.info("[fakenet] 桩已起 %s（profile=%s）", name, profile)
        return stub_host(task_id)
    except Exception as e:                # noqa: BLE001 —— fail-open
        log.warning("[fakenet] 桩启动失败 %s（fail-open）：%s", name, e)
        return None


def cleanup_stub(client, task_id) -> None:
    """删桩容器（幂等；网络不删=共享资源）。"""
    try:
        c = client.containers.get(stub_name(task_id))
        c.remove(force=True)
        log.info("[fakenet] 桩已清 %s", stub_name(task_id))
    except Exception:                     # noqa: BLE001 —— 已不在=幂等
        pass
