# -*- coding: utf-8 -*-
"""A4 新项目拉取：codeload tarball 通道（git clone 被代理阻断的既证事实下唯一可用通道）。
用法：python fetch_arm4_projects.py
产物：data/candidates/<name>/（已存在则跳过）
"""
import io
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\test agent_docker")
CAND = ROOT / "data" / "candidates"
PROJECTS = [
    ("inflection", "jpvanhal/inflection", "master"),
    ("humanize", "python-humanize/humanize", "main"),
    ("wcwidth", "jquast/wcwidth", "master"),
]


def fetch(name: str, repo: str, branch: str) -> int:
    dest = CAND / name
    if dest.exists() and any(dest.iterdir()):
        print(f"[skip] {name} 已存在")
        return 0
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
    print(f"[fetch] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "e9a4-fetch"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        top = members[0].name.split("/")[0]
        tf.extractall(CAND)
    src = CAND / top
    src.rename(dest)
    print(f"[ok] {name} <- {repo}@{branch} ({len(data)//1024}KB, {time.time()-t0:.1f}s)")
    return 0


if __name__ == "__main__":
    for name, repo, branch in PROJECTS:
        try:
            fetch(name, repo, branch)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {name}: {type(e).__name__}: {e}")
    print("[done]", [p.name for p in sorted(CAND.iterdir())])
