# -*- coding: utf-8 -*-
"""GitHub 项目批量下载（codeload zip 通道，无代理可用——E9 先例）→ 解压进 D:\\test program。"""
import io
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests

TP = Path(r"D:\test program")

# (lang, owner/repo, 本地目录名, 分支候选)
REPOS = [
    ("python", "TheAlgorithms/Python", "python-thealgorithms", ["master", "main"]),
    ("python", "keleshev/schema", "python-schema", ["master", "main"]),
    ("python", "benjaminp/six", "python-six", ["master", "main"]),
    ("python", "un33k/python-slugify", "python-slugify", ["master", "main"]),
    ("python", "jmoiron/humanize", "python-humanize", ["master", "main"]),
    ("go", "google/uuid", "go-uuid", ["master", "main"]),
    ("go", "spf13/cast", "go-cast", ["master", "main"]),
    ("go", "fatih/color", "go-color", ["master", "main"]),
    ("go", "caarlos0/env", "go-env", ["master", "main", "v11"]),
    ("go", "go-ini/ini", "go-ini", ["master", "main"]),
    ("java", "google/gson", "java-gson", ["master", "main"]),
    ("java", "stleary/JSON-java", "java-json", ["master", "main"]),
    ("js", "iamkun/dayjs", "js-dayjs", ["master", "main", "dev"]),
    ("js", "lodash/lodash", "js-lodash", ["main", "master"]),
    ("js", "uuidjs/uuid", "js-uuid", ["main", "master"]),
    ("php", "composer/semver", "php-semver", ["main", "master"]),
    ("php", "nikic/FastRoute", "php-fastroute", ["master", "main"]),
    ("php", "ramsey/collection", "php-collection", ["main", "master"]),
    ("ts", "sindresorhus/slugify", "ts-slugify", ["main", "master"]),
    ("ts", "sindresorhus/ky", "ts-ky", ["main", "master"]),
]


def fetch(owner_repo: str, branches: list[str]) -> bytes | None:
    for br in branches:
        url = f"https://codeload.github.com/{owner_repo}/zip/refs/heads/{br}"
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
            print(f"    {br}: HTTP {r.status_code}")
        except Exception as e:
            print(f"    {br}: {type(e).__name__} {str(e)[:80]}")
        time.sleep(1)
    return None


def main() -> int:
    fails = []
    for lang, repo, dirname, branches in REPOS:
        dest = TP / lang / dirname
        if dest.exists():
            print(f"[skip] {dirname} 已存在")
            continue
        print(f"[下载] {repo} → {lang}/{dirname}", flush=True)
        data = fetch(repo, branches)
        if not data:
            print(f"  [FAIL] {repo} 三分支全 404")
            fails.append(repo)
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            tmp = dest.parent / f"_tmp_{dirname}"
            if tmp.exists():
                shutil.rmtree(tmp)
            zf.extractall(tmp)
            # zip 内层目录（repo-branch）
            inner = [d for d in tmp.iterdir() if d.is_dir()]
            src = inner[0] if inner else tmp
            shutil.move(str(src), str(dest))
            shutil.rmtree(tmp, ignore_errors=True)
            n = sum(1 for _ in dest.rglob("*") if _.is_file())
            print(f"  OK: {n} 文件")
        except Exception as e:
            print(f"  [FAIL 解压] {type(e).__name__}: {str(e)[:100]}")
            fails.append(repo)
        time.sleep(1)
    print("\n==== 失败清单 ====" if fails else "\n==== 全部成功 ====")
    for f in fails:
        print(" ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
