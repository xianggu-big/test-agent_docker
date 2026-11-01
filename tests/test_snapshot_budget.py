"""任务B（2026-11-01 任务卡）单测：快照排除/单文件上限/总量 fail-fast/流式 tar 结构。
tmp 目录造小文件 + 显式小预算（不真 7GB）；FakeContainer 验证 put_archive 收到合法 tar。"""
import tarfile
import io

import pytest

from test_agent.sandbox.runner import snapshot_plan, SandboxRunner


def _mkws(tmp_path, files: dict[str, int], gitignore: str | None = None):
    """造工作区：{相对路径: 字节数}；gitignore 非空时写顶层 .gitignore。"""
    ws = tmp_path / "ws"
    for rel, size in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    if gitignore is not None:
        (ws / ".gitignore").write_bytes(gitignore.encode("utf-8"))  # 写死 \n，防 Windows 转义
    return ws


def test_gitignore_尊重_大目录被排除且不计总量(tmp_path):
    ws = _mkws(tmp_path, {"src/a.py": 10, "src/b.py": 20, "data/big.bin": 5000},
               gitignore="data/\n")
    plan = snapshot_plan(ws, max_total=100, max_file=10_000)
    names = [str(f.relative_to(ws)).replace("\\", "/") for f in plan["files"]]
    assert names == [".gitignore", "src/a.py", "src/b.py"]
    assert plan["total_bytes"] == 10 + 20 + len("data/\n")
    assert ("data" in [d for d, _ in plan["excluded_dirs"]]) or \
           ("data" in [d.split("/")[0] for d, _ in plan["excluded_dirs"]])
    assert plan["gitignore_used"] is True


def test_内置排除_node_modules_等不进快照(tmp_path):
    ws = _mkws(tmp_path, {"app.py": 5, "node_modules/pkg/index.js": 9999,
                          "__pycache__/x.pyc": 9999, ".git/config": 9999})
    plan = snapshot_plan(ws, max_total=10_000, max_file=10_000)
    names = [str(f.relative_to(ws)).replace("\\", "/") for f in plan["files"]]
    assert names == ["app.py"]
    excluded = {d for d, _ in plan["excluded_dirs"]}
    assert "node_modules" in excluded and "__pycache__" in excluded and ".git" in excluded


def test_单文件超限_跳过并记录(tmp_path):
    ws = _mkws(tmp_path, {"small.py": 10, "huge.bin": 5_000_000})
    plan = snapshot_plan(ws, max_total=10_000_000, max_file=1_000)
    names = [str(f.relative_to(ws)).replace("\\", "/") for f in plan["files"]]
    assert "huge.bin" not in names and "small.py" in names
    assert plan["skipped_files"] == [("huge.bin", 5_000_000)]


def test_总量超限_fail_fast_错误信息可行动(tmp_path):
    ws = _mkws(tmp_path, {"a.bin": 8_000, "b.bin": 3_000})
    with pytest.raises(RuntimeError) as ei:
        snapshot_plan(ws, max_total=1_000, max_file=100_000)
    msg = str(ei.value)
    assert "快照预算超限" in msg and "a.bin" in msg          # 最大纳入项点名
    assert ".gitignore" in msg and "SNAPSHOT_MAX_TOTAL_BYTES" in msg  # 处理建议


def test_copy_dir_into_流式tar结构合法(tmp_path, monkeypatch):
    """FakeContainer 收 put_archive 的文件句柄 → 读回验证是合法 tar 且含预期文件；
    monkeypatch 掉 docker 客户端构造（SandboxRunner 构造即连 Docker 的坑）。"""
    ws = _mkws(tmp_path, {"src/a.py": 6, "src/deep/b.py": 7}, gitignore="data/\n")
    monkeypatch.setattr("docker.from_env", lambda: object())

    class FakeContainer:
        def __init__(self):
            self.archives = []
            self.chowns = []

        def put_archive(self, path, fh):
            self.archives.append((path, fh.read()))   # 读回=模拟 daemon 收流

        def exec_run(self, cmd, **kw):
            self.chowns.append(cmd)

    fake = FakeContainer()
    runner = SandboxRunner()
    summary = runner.copy_dir_into(fake, ws)
    assert summary["file_count"] == 3 and summary["gitignore_used"] is True
    assert fake.chowns and "chown" in " ".join(fake.chowns[0])
    path, blob = fake.archives[0]
    assert path == "/work"
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        members = {m.name for m in tar.getmembers()}
    assert "src/a.py" in members and "src/deep/b.py" in members  # 深层父目录由解包侧自动建


def test_快照预算默认值_来自config(tmp_path):
    from test_agent import config
    ws = _mkws(tmp_path, {"a.py": 1})
    plan = snapshot_plan(ws)   # 不传预算 → 走 config 默认（512MB/64MB）
    assert plan["total_bytes"] == 1 and plan["gitignore_used"] is False
    assert config.SNAPSHOT_MAX_TOTAL_BYTES == 512 * 1024 * 1024
