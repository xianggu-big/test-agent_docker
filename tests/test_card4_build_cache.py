"""卡 4 测试锁：构建缓存持久化（2026-11-15，冷缓存家族 1139/1140/1142/1143-1145 总解）。

binds=go126 挂 GOMODCACHE(/go/pkg/mod)+GOCACHE(/root/.cache/go-build)、jdk17-maven
挂 /home/tester/.m2——fresh 重建/跨任务共享缓存；provision 预热步（go mod download /
mvn dependency:go-offline，900s fail-open）。
"""
from pathlib import Path

from test_agent import config
from test_agent.graph.nodes import cache_binds_for, warm_build_cache
from test_agent.sandbox.runner import SandboxRunner


# ---------- ① binds 组装 ----------

def test_cache_binds_go():
    binds = cache_binds_for(config.IMAGE_GO)
    assert binds, "go 镜像必须有缓存挂载"
    host_paths = {v["bind"]: k for k, v in binds.items()}
    assert "/go/pkg/mod" in host_paths and "/root/.cache/go-build" in host_paths
    for hp in binds:
        assert hp.startswith(str(config.BUILD_CACHE_HOST_DIR)), "宿主目录必须在 data/build_cache 下"
        assert Path(hp).exists(), "宿主缓存目录必须已建（否则挂载生成匿名卷）"


def test_cache_binds_java():
    binds = cache_binds_for(config.IMAGE_JAVA)
    assert "/home/tester/.m2" in {v["bind"] for v in binds.values()}


def test_cache_binds_none_for_other_images():
    assert cache_binds_for(config.IMAGE_PY) is None
    assert cache_binds_for("unknown:image") is None


# ---------- ② ensure_container 透传 binds ----------

class _FakeContainers:
    def __init__(self, store):
        self._store = store

    def get(self, name):
        raise Exception("NotFound")

    def run(self, image, name=None, command=None, detach=False, nano_cpus=None,
            mem_limit=None, volumes=None, **kw):
        self._store["run_kwargs"] = dict(volumes=volumes, image=image, name=name)
        return object()


class _FakeClient:
    def __init__(self, store):
        self.containers = _FakeContainers(store)


def test_ensure_container_passes_binds(tmp_path, monkeypatch):
    from docker.errors import NotFound as _NF
    store = {}
    r = SandboxRunner.__new__(SandboxRunner)
    r.client = _FakeClient(store)
    # NotFound 抛出类型必须匹配 runner 的 except NotFound——monkeypatch get 抛真类型
    def _get(name):
        raise _NF("no such container")
    r.client.containers.get = _get
    vols = {"/h": {"bind": "/c", "mode": "rw"}}
    r.ensure_container("img:x", "mta-run-1", volumes=vols)
    assert store["run_kwargs"]["volumes"] == vols, "volumes 必须透传到 containers.run"
    r.ensure_container("img:x", "mta-run-2")
    assert store["run_kwargs"]["volumes"] is None, "不传 volumes 零行为变化"


# ---------- ③ 预热步 ----------

class _WarmR:
    def __init__(self, exit_code=0):
        self._e = exit_code
        self.cmds = []

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        self.cmds.append((cmd, timeout_s))
        class _Res:
            exit_code = self._e
            stdout = "ok"
            stderr = ""
        return _Res()


def test_warm_build_cache_go():
    r = _WarmR()
    msg = warm_build_cache(r, object(), "go")
    assert msg and "预热完成" in msg
    cmd, timeout_s = r.cmds[0]
    joined = " ".join(cmd)
    assert "go mod download" in joined and timeout_s == 900
    # 2026-11-17 卡 2（1272-1278 实锤）：旧版 `| tail -3` 管道把退出码变成 tail 的 0，
    # mvn/go 失败被吞成"预热完成"——命令不得再带管道，fail-open 由宿主侧消息层保证
    assert "|" not in joined, "预热命令不得用管道掩蔽退出码"
    assert "|| true" not in joined


def test_warm_build_cache_java():
    r = _WarmR()
    msg = warm_build_cache(r, object(), "java")
    assert msg and "go-offline" in msg
    joined = " ".join(r.cmds[0][0])
    assert "dependency:go-offline" in joined and "jacoco" in joined,         "java 预热必须连 jacoco plugin 一起拉（go-offline 不覆盖命令行 goal 的 plugin）"


def test_warm_build_cache_fail_open():
    r = _WarmR(exit_code=1)
    msg = warm_build_cache(r, object(), "go")
    assert msg and "预热未完成" in msg and "exit=1" in msg, "失败如实留痕但不阻断"


def test_warm_build_cache_failure_message_carries_output_tail():
    """1272 谎报家族回归锁：失败消息必须带输出尾部（MissingProjectException 可见）。"""
    r = _WarmR(exit_code=1)
    orig = r.exec_in

    def exec_in(container, cmd, workdir="/work", timeout_s=30):
        res = orig(container, cmd, workdir, timeout_s)
        res.stdout = "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MissingProjectException"
        return res
    r.exec_in = exec_in
    msg = warm_build_cache(r, object(), "java")
    assert "MissingProjectException" in msg, "失败输出尾部必须进留痕消息"


def test_warm_build_cache_none_for_other():
    assert warm_build_cache(_WarmR(), object(), "python") is None
    assert warm_build_cache(_WarmR(), object(), "php") is None
