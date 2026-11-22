# -*- coding: utf-8 -*-
"""任务1（2026-11-22 零 API 工作包）：BugsInPy 扩量准备层——staging+digest 封存+P0 预检。

真源：docs/零API工作包设计与交接-20261122.md 任务 1 + data/benchmarks/manifests/py-*.json
（12 格双 ref 完备，遗憾清偿 2026-11-19 件 2 预备产物；本地 BugsInPy-master 在
data/benchmarks/repos/BugsInPy-master 提供 bug.info 真值）。

三层职责（全部零 LLM）：
  stage    ：codeload 双 ref tarball → 解压 staging/bugsinpy-{repo}-bug{N}/{buggy,fixed}
             + tarball sha256 封存回 manifest（防 rug pull，对齐 D2 digest 纪律）
  precheck ：容器内（mta-test:py312）SWT 口径 fail-to-pass 预检——
             fixed 侧 pip install . + 官方测试必过；buggy 侧 pip install . +
             拷 fixed 版测试文件（buggy 快照无失败用例——SWT-bench 口径）+ 必败。
             败因=断言失败才算 PASS（ImportError/collection error=环境边界，如实弃）。
  healthcard：P0 结果落 JSON（status=ok/blocked，过不了的换下一个不硬塞）。

用法：
  python -I scripts/stage_bugsinpy_batch.py --stage        # 仅 staging+digest
  python -I scripts/stage_bugsinpy_batch.py --precheck      # 全格 P0 预检（含 stage 幂等）
  python -I scripts/stage_bugsinpy_batch.py --precheck --only py-tqdm-bug1

坑（交接文档+先例）：
  - git clone 被代理挡——只用 codeload tarball（HTTP 200 已验证）
  - tar 解压 --strip-components=1（tqdm bug9 先例）
  - 部分老项目在 py312 的 setuptools/pytest 兼容炸=环境边界，如实弃不硬塞
  - sanic bug1 python_version=3.6：py312 容器过不了属预期内，P0 如实记录
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST_DIR = ROOT / "data/benchmarks/manifests"
REPOS_DIR = ROOT / "data/benchmarks/repos"
STG = ROOT / "data/benchmarks/staging"
BUGSINPY_LOCAL = REPOS_DIR / "BugsInPy-master"
HEALTH_DIR = STG / "healthcards"
IMG = "mta-test:py312"
SKIP = {"py-requests-smoke"}          # example 占位（无双 ref）


def load_cells(only: str | None = None) -> list[dict]:
    out = []
    for p in sorted(MANIFEST_DIR.glob("py-*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        if m["id"] in SKIP:
            continue
        if not (m.get("buggy_ref") and m.get("fixed_ref")):
            continue
        if only and m["id"] != only:
            continue
        m["_path"] = p
        out.append(m)
    return out


def bug_no(m: dict) -> str:
    return m["id"].rsplit("-bug", 1)[1]


def stage_one(m: dict) -> str:
    """双 tarball 拉取+解压+digest 封存。返回 'staged'/'cached'。"""
    repo = m["repo"].rstrip("/").rsplit("/", 1)[-1]
    d = STG / f"bugsinpy-{repo}-bug{bug_no(m)}"
    (d / "buggy").mkdir(parents=True, exist_ok=True)
    (d / "fixed").mkdir(parents=True, exist_ok=True)
    digests = {}
    for side, ref in (("buggy", m["buggy_ref"]), ("fixed", m["fixed_ref"])):
        marker = d / f".{side}_staged_{ref[:10]}"
        if marker.exists() and any((d / side).iterdir()):
            digests[side] = json.loads((d / f".{side}_digest.json").read_text())["sha256"]
            continue
        # codeload 形态（交接文档坑段先例）：/tar.gz/{ref}——/archive/{ref}.tar.gz 会 404
        url = f"{m['repo'].rstrip('/')}/tar.gz/{ref}".replace(
            "github.com", "codeload.github.com", 1)
        tar_p = REPOS_DIR / f"{repo}-{side}-{ref[:10]}.tar.gz"
        for attempt in range(3):
            try:
                urllib.request.urlretrieve(url, tar_p)
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"codeload 拉取失败 {url}: {e}")
                time.sleep(2 * (attempt + 1))
        digests[side] = hashlib.sha256(tar_p.read_bytes()).hexdigest()
        # 只解压 py 相关成员（排 .git_pipe/大目录无关物）——tarfile 流式+strip 1。
        # Windows MAX_PATH 坑（cookiecutter×3 实锤）：测试夹具里 {{cookiecutter.x}}
        # Jinja 占位符嵌套目录超 260 字符 → WinError 206——跳过超长成员并留痕
        # （官方失败用例是否依赖这些夹具由 P0 预检裁决，不在此硬猜）
        skipped = []
        skip_prefixes = []
        with tarfile.open(tar_p) as tf:
            for mem in tf.getmembers():
                parts = mem.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1]
                # 长度检查用原始 mem.name（tf.extract 按原名落盘，根前缀 41 字符）；
                # 父目录被跳过的成员一并跳过（extract 会重建父链→同样炸 206）
                too_long = len(str(d / side / mem.name)) > 235
                under_skipped = any(rel.startswith(p) for p in skip_prefixes)
                if too_long or under_skipped:
                    skipped.append(rel)
                    if mem.isdir():
                        skip_prefixes.append(rel + "/")
                    continue
                tf.extract(mem, d / side, filter="data")
                # strip-components=1：把 {root}/x 落到 side/x
                dst = d / side / rel
                src = d / side / mem.name
                if src != dst:
                    if src.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
        # 清掉解压根目录残留（strip 后应只剩空壳）
        for root_dir in (d / side).iterdir():
            if root_dir.name == parts[0]:
                shutil.rmtree(root_dir, ignore_errors=True)
                break
        (d / f".{side}_digest.json").write_text(
            json.dumps({"sha256": digests[side], "ref": ref, "url": url,
                        "skipped_long_path": skipped[:20],
                        "skipped_n": len(skipped)}, ensure_ascii=False), encoding="utf-8")
        marker.touch()
    # digest 封存回 manifest（幂等：已有且一致不动）
    cur = m.get("tarball_sha256") or {}
    if cur.get("buggy") != digests["buggy"] or cur.get("fixed") != digests["fixed"]:
        m2 = json.loads(m["_path"].read_text(encoding="utf-8"))
        m2["tarball_sha256"] = digests
        m2["status"] = "staged"
        m["_path"].write_text(json.dumps(m2, ensure_ascii=False, indent=1), encoding="utf-8")
        return "staged"
    return "cached"


def parse_test(m: dict) -> tuple[str, str]:
    """official_test_cmd → (test_file 相对路径, 用例 nodeid 或 '')。

    全量 pytest 形态（cookiecutter 实锤 'python -m pytest -q' 无文件名）：
    从本地 BugsInPy-master 的 bug.info test_file 字段兜底——
    P0 只需要 test_file（SWT 口径拷 fixed 版该文件套 buggy 源码）。"""
    cmd = m["official_test_cmd"]
    nodeid = ""
    mm = re.search(r"([\w./-]+\.py)(?:::{1,2}[\w./-]+)?", cmd)
    if mm:
        tf = mm.group(1)
        m2 = re.search(r"(\.py::.+)$", cmd.strip())
        if m2:
            nodeid = m2.group(1)
    else:
        repo = m["repo"].rstrip("/").rsplit("/", 1)[-1]
        info = BUGSINPY_LOCAL / "projects" / repo / "bugs" / bug_no(m) / "bug.info"
        if not info.exists():
            raise RuntimeError(f"无法解析测试目标且无 bug.info: {cmd}")
        fm = re.search(r'test_file="([^"]+)"', info.read_text(encoding="utf-8"))
        if not fm:
            raise RuntimeError(f"bug.info 无 test_file: {info}")
        tf = fm.group(1)
    return tf, nodeid


def _docker(args: list[str], timeout: int = 420) -> tuple[int, str]:
    r = subprocess.run(["docker"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def precheck_one(m: dict, cname: str) -> dict:
    """容器内 SWT 口径：fixed 必过 + buggy（拷 fixed 测试文件）必败（断言败）。"""
    repo = m["repo"].rstrip("/").rsplit("/", 1)[-1]
    d = STG / f"bugsinpy-{repo}-bug{bug_no(m)}"
    tf, nodeid = parse_test(m)
    card = {"id": m["id"], "test_file": tf, "nodeid": nodeid or None,
            "python_version_manifest": None, "status": "blocked", "detail": ""}
    # bug.info 的 python_version 线索（老版本预期标注，不拦截）
    info = BUGSINPY_LOCAL / "projects" / repo / "bugs" / bug_no(m) / "bug.info"
    if info.exists():
        pv = re.search(r'python_version="([\d.]+)"', info.read_text())
        if pv:
            card["python_version_manifest"] = pv.group(1)
    try:
        _docker(["rm", "-f", cname], timeout=30)
        _docker(["run", "-d", "--name", cname, IMG, "sleep", "infinity"], timeout=60)
        results = {}
        for side in ("fixed", "buggy"):
            _docker(["exec", cname, "sh", "-c",
                     f"rm -rf /ws_{side} && mkdir -p /ws_{side}"], timeout=30)
            # 拷贝 staging 源码进容器（docker cp 顶层内容）
            r = subprocess.run(["docker", "cp", str(d / side) + "/.", f"{cname}:/ws_{side}"],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                card["detail"] = f"docker cp {side} 失败: {r.stderr[:200]}"
                return card
            # SWT 口径：buggy 侧用 fixed 版的测试文件（失败用例随 fix 提交新增）
            if side == "buggy":
                r = subprocess.run(
                    ["docker", "cp", str(d / "fixed" / tf), f"{cname}:/ws_buggy/{tf}"],
                    capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    card["detail"] = f"拷 fixed 测试文件失败（fixed 缺 {tf}?）: {r.stderr[:160]}"
                    return card
            # setup：容忍失败（老项目 py312 构建炸属常态——源码 import 路径仍可能
            # 可用），只记录 warning 不阻断；rc 保真（管道吃退出码教训：tail 的 rc
            # 恒 0 曾把 setup 失败/测试败全判成"过"）。
            # extra_deps（P0 全量实锤后加）：老项目测试依赖 pin（click<8.1/pytest-httpbin/
            # multidict 等）——py312 容器跑 2019 生态的版本地狱修补，装失败同样容忍。
            # shell 解析坑（第二轮实锤）：spec 含 '<'/';'/引号必须逐个单引号包裹——
            # 裸拼会把 'jinja2<3.1' 当重定向、'pkg; marker' 当命令分隔
            extra = m.get("extra_deps") or []
            extra_q = " ".join(f"'{d}'" for d in extra)
            pre = (f"python -m pip install --quiet {extra_q} > /tmp/deps_out 2>&1; "
                   if extra else "")
            setup = m.get("setup_cmd") or "python -m pip install . --quiet"
            rc, out = _docker(["exec", "-w", f"/ws_{side}", cname, "sh", "-c",
                               f"{pre}{setup} > /tmp/setup_out 2>&1; rc=$?; "
                               f"tail -3 /tmp/setup_out; exit $rc"], timeout=420)
            if rc != 0:
                card.setdefault("setup_warn", {})[side] = out[-160:]
            tcmd = m["official_test_cmd"]
            # 归一：非 python -m pytest 开头的（如 'pytest ...'）统一补 python -m；
            # 全量形态（无 .py 文件）收窄到 bug.info 的 test_file（P0 只验目标文件，
            # 全仓库跑=慢+无关失败干扰断言败因判定）
            if tcmd.strip().startswith("pytest"):
                tcmd = "python -m " + tcmd.strip()
            if not re.search(r"\.py", tcmd):
                tcmd = f"python -m pytest -q {tf}" + (nodeid or "")
            rc, out = _docker(["exec", "-w", f"/ws_{side}", "-e", "PYTHONPATH=/ws_{side}",
                               cname, "sh", "-c",
                               f"{tcmd} > /tmp/test_out 2>&1; rc=$?; "
                               f"tail -25 /tmp/test_out; exit $rc"], timeout=420)
            results[side] = (rc, out)
        rc_f, out_f = results["fixed"]
        rc_b, out_b = results["buggy"]
        fixed_pass = (rc_f == 0)
        buggy_fail = (rc_b != 0)
        # 败因分类：环境边界（nose 等老依赖 py312 装不了/collection error）≠ 断言败
        ENV_MARKS = ("ModuleNotFoundError", "ImportError", "ERROR collecting",
                     "no collectors", "SyntaxError")
        env_blocked = any(k in out_b for k in ENV_MARKS)
        assertion_fail = buggy_fail and not env_blocked and (
            "failed" in out_b or "FAILED" in out_b or "assert" in out_b.lower())
        if fixed_pass and buggy_fail and assertion_fail:
            card["status"] = "ok"
            card["detail"] = (f"fixed 过(rc=0)；buggy 断言败 tail={out_b[-160:]!r}")
        else:
            why = []
            if not fixed_pass:
                why.append(f"fixed 未过 rc={rc_f} tail={out_f[-140:]!r}")
            if not buggy_fail:
                why.append(f"buggy 未败 rc={rc_b}（bug 信号缺失）tail={out_b[-120:]!r}")
            if buggy_fail and not assertion_fail:
                kind = "环境边界（老依赖 py312 不可用/collect 炸）" if env_blocked else "非断言败"
                why.append(f"buggy 败因={kind} tail={out_b[-140:]!r}")
            card["status"] = "blocked"
            card["detail"] = "；".join(why)
    except subprocess.TimeoutExpired:
        card["detail"] = "预检超时（420s）"
    finally:
        _docker(["rm", "-f", cname], timeout=30)
    return card


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true", help="仅 staging+digest")
    ap.add_argument("--precheck", action="store_true", help="P0 预检（含幂等 staging）")
    ap.add_argument("--only", help="只处理该 manifest id")
    args = ap.parse_args()
    if not (args.stage or args.precheck):
        ap.error("需要 --stage 或 --precheck")
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    cells = load_cells(args.only)
    print(f"[batch] {len(cells)} 格")
    for m in cells:
        try:
            state = stage_one(m)
        except Exception as e:
            print(f"[stage] {m['id']}: FAIL {e}")
            continue
        print(f"[stage] {m['id']}: {state}")
        if args.precheck:
            cname = f"mta-p0-{m['id'][-12:].replace('-', '')}-{int(time.time())%10000}"
            card = precheck_one(m, cname)
            (HEALTH_DIR / f"{m['id']}.json").write_text(
                json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[precheck] {m['id']}: {card['status'].upper()} {card['detail'][:120]}")
    print("[done] healthcards →", HEALTH_DIR)


if __name__ == "__main__":
    main()
