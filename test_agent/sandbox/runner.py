"""L4 沙箱执行器：常驻容器 + docker exec 驱动。

约定（对齐借用框架 mta-sandbox 的模式）：
- 容器常驻（sleep infinity），每次执行用 exec，任务结束统一回收
- 资源限额在启动参数里（--cpus / --memory），镜像保持薄
- 执行与判定分离：容器里只跑测试，判读由宿主侧做，容器不给特权

诚实边界：docker SDK 的 exec_run 本身不支持超时中断，这里用线程 +
future.result(timeout) 实现"超时返回"，超时后底层 exec 仍会占用直到
容器回收 —— 因此超时任务由调用方（L4 队列）负责 cleanup，不得复用。
"""
import io
import logging
import tarfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import BuildError, ImageNotFound, NotFound

from .. import config

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxRunner:
    """一个 Runner 管理若干常驻容器；容器名 = mta-run-<task_id>。"""

    def __init__(self):
        self.client = docker.from_env()

    # —— 生命周期 ——

    def ensure_container(self, image: str, name: str,
                         cpu_limit: float = config.DEFAULT_CPU_LIMIT,
                         mem_mb: int = config.DEFAULT_MEM_LIMIT_MB,
                         fresh: bool = False):
        """确保常驻容器在跑（幂等：同名容器存在则复用）。

        fresh=True 时先销毁同名旧容器再新建——用于超时后重建（A2）与脏容器场景（A3）；
        普通 R-Loop 轮次不传 fresh，保留容器内的依赖缓存。
        """
        try:
            c = self.client.containers.get(name)
            if fresh:
                c.remove(force=True)
                log.info("已销毁旧容器 %s（fresh 重建）", name)
            else:
                if c.status != "running":
                    c.start()
                return c
        except NotFound:
            pass
        log.info("启动沙箱容器 %s (image=%s, cpu=%s, mem=%sm)", name, image, cpu_limit, mem_mb)
        return self.client.containers.run(
            image,
            name=name,
            command=["sleep", "infinity"],
            detach=True,
            nano_cpus=int(cpu_limit * 1e9),
            mem_limit=f"{mem_mb}m",
        )

    def image_exists(self, image: str) -> bool:
        """本地是否已有该镜像（只探测不构建，沙箱自适应模式①的判定用）。"""
        try:
            self.client.images.get(image)
            return True
        except Exception:                     # noqa: BLE001 —— ImageNotFound 及驱动差异
            return False

    def ensure_image(self, image: str, buildargs: dict | None = None) -> bool:
        """本地缺镜像时按注册表自动构建（"自动拉取容器"闭环）。返回镜像是否就绪。"""
        try:
            self.client.images.get(image)
            return True
        except ImageNotFound:
            pass
        ctx = config.IMAGE_BUILD_CTX.get(image)
        if ctx is None:
            log.error("镜像 %s 不在构建注册表，无法自动补齐", image)
            return False
        log.info("本地缺镜像 %s，开始自动构建 %s", image, ctx)
        try:
            self.client.images.build(path=str(ctx), tag=image, rm=True,
                                     buildargs=buildargs or {})
        except BuildError as e:
            log.error("镜像 %s 构建失败: %s", image, e)
            return False
        return True

    def cleanup(self, name: str) -> None:
        """停掉并删除容器（幂等）。"""
        try:
            c = self.client.containers.get(name)
            c.remove(force=True)
            log.info("已回收容器 %s", name)
        except NotFound:
            pass

    # —— 快照落地 ——

    def copy_dir_into(self, container, src_dir: str | Path, dest_path: str = "/work") -> None:
        """把宿主目录快照进容器（打 tar → put_archive），随后 chown 给 tester。"""
        src = Path(src_dir)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in src.rglob("*"):
                tar.add(p, arcname=p.relative_to(src))
        buf.seek(0)
        container.put_archive(dest_path, buf)
        # put_archive 以 root 写入，收个归属让非 root 的 tester 可写（缓存/覆盖率文件）
        container.exec_run(["chown", "-R", "tester:tester", dest_path], user="root")

    # —— 执行 ——

    def exec_in(self, container, cmd: list[str],
                workdir: str = "/work",
                timeout_s: int = config.DEFAULT_JOB_TIMEOUT_S) -> ExecResult:
        """容器内执行命令；镜像 USER 已是 tester，无需再降权。"""
        future = _pool.submit(container.exec_run, cmd, workdir=workdir, demux=True)
        try:
            code, (out, err) = future.result(timeout=timeout_s)
        except FutureTimeout:
            log.warning("执行超时(%ss): %s", timeout_s, cmd)
            return ExecResult(exit_code=-1, stdout="", stderr=f"timeout after {timeout_s}s: {' '.join(cmd)}")
        return ExecResult(
            exit_code=code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
        )
