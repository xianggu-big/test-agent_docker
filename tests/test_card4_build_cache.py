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
            mem_limit=None, binds=None, **kw):
        self._store["run_kwargs"] = dict(binds=binds, image=image, name=name)
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
    binds = {"/h": {"bind": "/c", "mode": "rw"}}
    r.ensure_container("img:x", "mta-run-1", binds=binds)
    assert store["run_kwargs"]["binds"] == binds, "binds 必须透传到 containers.run"
    r.ensure_container("img:x", "mta-run-2")
    assert store["run_kwargs"]["binds"] is None, "不传 binds 零行为变化"


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
    assert "|| true" in joined, "预热命令必须 fail-open"


def test_warm_build_cache_java():
    r = _WarmR()
    msg = warm_build_cache(r, object(), "java")
    assert msg and "go-offline" in msg
    assert "dependency:go-offline" in " ".join(r.cmds[0][0])


def test_warm_build_cache_fail_open():
    r = _WarmR(exit_code=1)
    msg = warm_build_cache(r, object(), "go")
    assert msg and "预热未完成" in msg, "失败如实留痕但不阻断"


def test_warm_build_cache_none_for_other():
    assert warm_build_cache(_WarmR(), object(), "python") is None
    assert warm_build_cache(_WarmR(), object(), "php") is None
