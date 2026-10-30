# -*- coding: utf-8 -*-
"""clone 回退（D2）：codeload tarball 直连下载，git 失败不再炸任务。"""
import io
import tarfile
from pathlib import Path

import pytest

from test_agent.graph import nodes as N


def _tar_bytes(topdir: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name=f"{topdir}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_gh_url_parsing_variants():
    cases = {
        "https://github.com/a/b": ("a", "b"),
        "https://github.com/a/b.git": ("a", "b"),
        "https://github.com/a/b/": ("a", "b"),
        "http://www.github.com/a/b": ("a", "b"),
    }
    for url, want in cases.items():
        m = N._GH_URL.match(url)
        assert m and (m.group(1), m.group(2)) == want, url
    for bad in ("https://gitlab.com/a/b", "local://x", "/some/dir",
                "https://github.com/a"):
        assert not N._GH_URL.match(bad), bad


def test_extract_tarball_strip1_and_traversal_guard(tmp_path):
    data = _tar_bytes("repo-main", {
        "pkg/__init__.py": "X = 1\n",
        "pkg/mod.py": "def f():\n    return 2\n",
        "README.md": "hi\n",
    })
    # 混入一个路径穿越成员
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tf:
        d = b"evil"
        info = tarfile.TarInfo(name="repo-main/../../evil.txt")
        info.size = len(d)
        tf.addfile(info, io.BytesIO(d))
    mixed = _tar_bytes("repo-main", {"pkg/mod.py": "def f():\n    return 2\n"})
    # 分别验证：正常解包 + 穿越丢弃（用含恶意成员的包）
    assert N._extract_tarball_strip1(data, tmp_path) == 3
    assert (tmp_path / "pkg" / "__init__.py").read_text(encoding="utf-8") == "X = 1\n"
    assert not (tmp_path / "repo-main").exists()      # 顶层目录已剥
    assert N._extract_tarball_strip1(evil.getvalue(), tmp_path) == 0  # 只剩穿越成员，全拒
    assert not (tmp_path.parent / "evil.txt").exists()
    assert N._extract_tarball_strip1(mixed, tmp_path) == 1


def test_codeload_fallback_success(tmp_path, monkeypatch):
    payload = _tar_bytes("more-itertools-main", {"more_itertools/__init__.py": "X = 1\n"})

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(self, req, timeout=None):
        return FakeResp(payload)

    monkeypatch.setattr(N.urllib.request.OpenerDirector, "open", fake_open)
    out = N._codeload_fallback("https://github.com/more-itertools/more-itertools.git", tmp_path)
    assert out and "main 分支" in out and "1 个文件" in out
    assert (tmp_path / "more_itertools" / "__init__.py").exists()


def test_codeload_fallback_branch_fallback_and_failure(tmp_path, monkeypatch):
    """main 404 → 试 master；两者皆败 → None（由调用方如实报错）。"""
    calls = []

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(self, req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "master" in url:
            return FakeResp(_tar_bytes("r-master", {"a.py": "x = 1\n"}))
        import urllib.error
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"404"))

    monkeypatch.setattr(N.urllib.request.OpenerDirector, "open", fake_open)
    out = N._codeload_fallback("https://github.com/o/r", tmp_path)
    assert out and "master 分支" in out
    assert len(calls) == 2 and calls[0].endswith("/main") and calls[1].endswith("/master")

    calls.clear()

    def always_fail(self, req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(N.urllib.request.OpenerDirector, "open", always_fail)
    assert N._codeload_fallback("https://github.com/o/r", tmp_path) is None


def test_codeload_fallback_non_github_none(tmp_path):
    assert N._codeload_fallback("https://gitlab.com/a/b", tmp_path) is None
    assert N._codeload_fallback("/local/path", tmp_path) is None
