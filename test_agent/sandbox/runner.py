"""L4 沙箱执行器：常驻容器 + docker exec 驱动。

约定（对齐借用框架 mta-sandbox 的模式）：
- 容器常驻（sleep infinity），每次执行用 exec，任务结束统一回收
- 资源限额在启动参数里（--cpus / --memory），镜像保持薄
- 执行与判定分离：容器里只跑测试，判读由宿主侧做，容器不给特权

诚实边界：docker SDK 的 exec_run 本身不支持超时中断，这里用线程 +
future.result(timeout) 实现"超时返回"，超时后底层 exec 仍会占用直到
容器回收 —— 因此超时任务由调用方（L4 队列）负责 cleanup，不得复用。
"""
import fnmatch
import io
import logging
import os
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import BuildError, ImageNotFound, NotFound

from .. import config

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4)


# —— 工作区快照：排除 + 预算 + 流式打包（任务B/2026-11-01，#281 MemoryError 根修） ——

def _load_gitignore_spec(src: Path):
    """读工作区顶层 .gitignore → pathspec 匹配器。v1 诚实边界：只认顶层
    .gitignore（#281 的 data/、node_modules/ 都在顶层，已够用），嵌套 .gitignore
    不递归；pathspec 未安装时返回 None（只走内置排除，不炸主链路）。"""
    gi = src / ".gitignore"
    if not gi.is_file():
        return None
    try:
        import pathspec  # 声明依赖见 requirements.txt；缺库降级不阻塞
    except ImportError:
        log.warning("pathspec 未安装，快照不解析 .gitignore（pip install pathspec 启用）")
        return None
    lines = [ln for ln in gi.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return pathspec.GitIgnoreSpec.from_lines("gitwildmatch", lines) if lines else None


def _dir_size(p: Path) -> int:
    """目录逻辑大小（纯 stat 走查，不读内容；出错按 0 计）。"""
    total = 0
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            try:
                total += (Path(root) / f).lstat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def snapshot_plan(src_dir: str | Path, max_total: int | None = None,
                  max_file: int | None = None,
                  exclude_dirs: set | None = None) -> dict:
    """快照预扫描（纯函数，不打 tar）：.gitignore + 内置排除 + 单文件/总量预算。

    返回 {files, total_bytes, skipped_files, excluded_dirs, gitignore_used}；
    总量超预算 raise RuntimeError（fail-fast：错误信息含最大纳入项/总量/上限/处理建议，
    绝不把 MemoryError 留给进程）。"""
    src = Path(src_dir)
    max_total = config.SNAPSHOT_MAX_TOTAL_BYTES if max_total is None else max_total
    max_file = config.SNAPSHOT_MAX_FILE_BYTES if max_file is None else max_file
    excludes = set(config.SNAPSHOT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)
    spec = _load_gitignore_spec(src)
    files: list[Path] = []
    dirs_kept: list[str] = []      # 目录相对路径（posix）：打 tar 时保留目录条目——
                                   # 空目录（logs/、fixture 目录）丢失会让应用/测试
                                   # 因目录不存在失败且无留痕（2026-11-15 闲时找bug）
    sizes: dict[str, int] = {}
    total = 0
    skipped_files: list[tuple[str, int]] = []
    excluded_dirs: list[tuple[str, int]] = []

    def _excluded(name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in excludes)

    for root, dirs, names in os.walk(src, onerror=lambda e: None):
        rel_root = Path(root).relative_to(src)
        keep = []
        for d in dirs:
            rel_d = (rel_root / d).as_posix()
            if _excluded(d) or (spec is not None and spec.match_file(rel_d + "/")):
                excluded_dirs.append((rel_d, _dir_size(Path(root) / d)))
                continue
            keep.append(d)
            dirs_kept.append(rel_d)
        dirs[:] = keep
        for f in names:
            rel_f = (rel_root / f).as_posix()
            if spec is not None and spec.match_file(rel_f):
                continue
            p = Path(root) / f
            try:
                sz = p.lstat().st_size
            except OSError:
                continue
            if sz > max_file:
                skipped_files.append((rel_f, sz))
                continue
            files.append(p)
            sizes[rel_f] = sz
            total += sz

    if total > max_total:
        top = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
        top_txt = "、".join(f"{k}({_fmt_bytes(v)})" for k, v in top)
        raise RuntimeError(
            f"工作区快照预算超限：排除后仍需打包 {_fmt_bytes(total)} > 上限 "
            f"{_fmt_bytes(max_total)}。最大的纳入项：{top_txt}。"
            f"处理建议：①在项目 .gitignore 排除大目录/大文件（快照尊重 .gitignore）"
            f"②调大 config.SNAPSHOT_MAX_TOTAL_BYTES 后重跑 ③清理工作区后重跑。"
            f"已排除目录：{[d for d, _ in excluded_dirs[:8]] or '（无）'}")

    return {"files": files, "dirs": dirs_kept, "total_bytes": total, "skipped_files": skipped_files,
            "excluded_dirs": excluded_dirs, "gitignore_used": spec is not None}


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
                         fresh: bool = False, binds: dict | None = None):
        """确保常驻容器在跑（幂等：同名容器存在则复用）。

        fresh=True 时先销毁同名旧容器再新建——用于超时后重建（A2）与脏容器场景（A3）；
        普通 R-Loop 轮次不传 fresh，保留容器内的依赖缓存。
        binds（2026-11-15 卡 4）：构建缓存 bind mount（docker-py binds 格式），仅
        新建路径生效；复用路径沿用旧容器既有挂载（缓存本来就在）。
        """
        try:
            c = self.client.containers.get(name)
            if fresh:
                c.remove(force=True)
                log.info("已销毁旧容器 %s（fresh 重建）", name)
            else:
                if c.status != "running":
                    c.start()
                    # Loop-2/D10（E7 任务167 实锤 exec 409）：start 后仍非 running
                    # （OOM/崩溃秒死）→ 销毁重建，绝不返回死容器
                    if hasattr(c, "reload"):
                        c.reload()
                    if getattr(c, "status", "running") != "running":
                        log.warning("容器 %s start 后仍为 %s，销毁重建", name, c.status)
                        c.remove(force=True)
                        return self._run_sandbox(image, name, cpu_limit, mem_mb, binds=binds)
                return c
        except NotFound:
            pass
        log.info("启动沙箱容器 %s (image=%s, cpu=%s, mem=%sm)", name, image, cpu_limit, mem_mb)
        return self._run_sandbox(image, name, cpu_limit, mem_mb, binds=binds)

    def _run_sandbox(self, image: str, name: str, cpu_limit: float, mem_mb: int,
                     binds: dict | None = None):
        """新建沙箱容器（幂等规格集中一处，Loop-2 抽出）。binds=构建缓存挂载（卡 4）。"""
        return self.client.containers.run(
            image,
            name=name,
            command=["sleep", "infinity"],
            detach=True,
            nano_cpus=int(cpu_limit * 1e9),
            mem_limit=f"{mem_mb}m",
            binds=binds,
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

    def copy_dir_into(self, container, src_dir: str | Path, dest_path: str = "/work",
                      keep_git: bool = False) -> dict:
        """把宿主目录快照进容器（预扫描排除+预算 → 临时文件流式 tar → put_archive
        文件句柄流式上传），随后 chown 给 tester。返回快照摘要（供 provision 节点
        写 agent_messages：跳过了什么、打了多大——界面可查，任务B 要求）。

        keep_git（2026-11-05，缺口②修复；开源先例 SWE-bench harness=仓库带 .git 进
        容器）：默认仍剥 .git（Task B 体积/安全语义不变）；差分闸场景显式 True——
        容器内 git checkout 对照 ref 需要 .git（前置=宿主工作区本身有 .git，本地
        目录快照类任务无解，见三闸真机验证报告）。git 操作为只读 ref 切换。

        根修背景（#281 实锤）：旧实现 io.BytesIO + 整目录 tar 在内存里拼，
        7.7GB 工作区直接把宿主进程顶到 MemoryError——现在 tar 落临时文件，
        峰值内存 = 单文件读缓冲，与工作区大小无关。"""
        excludes = (config.SNAPSHOT_EXCLUDE_DIRS - {".git"}) if keep_git else None
        plan = snapshot_plan(src_dir, exclude_dirs=excludes)
        src = Path(src_dir)
        fd, tmp = tempfile.mkstemp(suffix=".tar", prefix="mta_snap_")
        os.close(fd)
        try:
            with tarfile.open(tmp, "w") as tar:
                for d in plan.get("dirs") or []:
                    tar.add(src / d, arcname=d, recursive=False)   # 目录条目（含空目录）
                for p in plan["files"]:
                    tar.add(p, arcname=p.relative_to(src))
            with open(tmp, "rb") as fh:            # 文件句柄 → requests 流式上传
                container.put_archive(dest_path, fh)
        finally:
            os.unlink(tmp)
        # put_archive 以 root 写入，收个归属让非 root 的 tester 可写（缓存/覆盖率文件）
        container.exec_run(["chown", "-R", "tester:tester", dest_path], user="root")
        summary = {"total_bytes": plan["total_bytes"], "file_count": len(plan["files"]),
                   "skipped_files": plan["skipped_files"], "excluded_dirs": plan["excluded_dirs"],
                   "gitignore_used": plan["gitignore_used"]}
        if plan["skipped_files"] or plan["excluded_dirs"]:
            log.info("快照摘要 task-dir=%s：打包 %d 文件 %s；跳过单文件 %d；排除目录 %d "
                     "(gitignore=%s)", src, summary["file_count"], _fmt_bytes(plan["total_bytes"]),
                     len(plan["skipped_files"]), len(plan["excluded_dirs"]),
                     plan["gitignore_used"])
        return summary

    # —— 执行 ——

    def read_app_log_pair(self, container, path: str, tail_lines: int = 2000) -> tuple[str, str]:
        """（黑盒能力补齐阶段1·采集层）读容器内应用日志文件 + docker logs 兜底原文。

        返回 (file_text, docker_text) 两路原始文本——判源（file 优先 / docker_logs 兜底 /
        empty）由 skills.app_logs.choose_log_source 纯函数决定，层间不重复实现。
        采集只取事实不解释：文件缺失/为空时第一路为 ""，兜底缺席同理（不炸主链路）。"""
        r = self.exec_in(container, ["sh", "-c", f"cat '{path}' 2>/dev/null"], timeout_s=30)
        try:
            docker_text = (container.logs(tail=tail_lines) or b"").decode(
                "utf-8", errors="replace")
        except Exception as e:            # noqa: BLE001 —— 兜底缺席只留痕，采集继续
            log.warning("docker logs 兜底读取失败: %s", e)
            docker_text = ""
        return r.stdout or "", docker_text

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
        except docker.errors.APIError as e:
            # 容器级不可达结构化（2026-11-15 Go L2 实锤：容器运行中死亡→exec 409
            # "not running" 裸抛炸整图；NotFound 的 get() 已由调用方各自兜底）
            log.warning("容器执行不可达: %s: %s（cmd=%s）",
                        type(e).__name__, str(e)[:120], cmd[:2])
            return ExecResult(exit_code=-1, stdout="",
                              stderr=f"环境错误：容器执行不可达（{type(e).__name__}）"
                                     f"——容器已停止或被回收，本轮命令未执行: {' '.join(cmd[:2])}")
        return ExecResult(
            exit_code=code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
        )
