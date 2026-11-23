# -*- coding: utf-8 -*-
"""BugsInPy 3 格 replay 镜像重验（2026-11-23，零 LLM）。

背景：cookiecutter×2/sanic 三格真格跑在 mta-test:py38（image_override），但 replay
硬编码 py312 → 生成用例 import 炸（collection error）→ f_map 空 → fp=0 误判
not_detected。真格的生成用例与 buggy 侧逐用例结果都在 tmp 工作区（mkdtemp 未清）。

本脚本：找到各格最新 tmp 区 → 从 t.db 读 buggy 侧 b_map → staging fixed 拷 tmp
replay 目录+拷生成用例 → docker run 同格镜像（py38）→ junit 解析 f_map →
重算 fp/ff/pf → 重判 grade → 写回 bench3_results.json（带 reverify 留痕）。
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # data/reports/bench3/ → 仓库根
STG = ROOT / "data/benchmarks/staging"
OUT = Path(__file__).resolve().parent / "bench3_results.json"
CELLS = [
    ("cookiecutter-bug1", "cookiecutter", "1"),
    ("cookiecutter-bug2", "cookiecutter", "2"),
    ("sanic-bug1", "sanic", "1"),
]
TMP_ROOT = Path(os.environ.get("TEMP", "/tmp"))


def _norm(nid: str) -> str:
    return nid.replace("\\", "/").replace(".py::", "::").replace(".py:", "::").strip()


def find_tmp(name: str) -> Path | None:
    cands = sorted(glob.glob(str(TMP_ROOT / f"bench3_{name}_*")), key=os.path.getmtime)
    return Path(cands[-1]) if cands else None


def buggy_map(tmp_dir: Path) -> tuple[dict, int, list[str]]:
    db = tmp_dir / "t.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM test_tasks ORDER BY id DESC LIMIT 1").fetchone()["id"]
    b_map = {}
    for r in conn.execute(
            "SELECT nodeid, status FROM test_case_results WHERE task_id=? "
            "ORDER BY round_no, id", (tid,)):
        b_map[_norm(r["nodeid"])] = r["status"]
    failed_n = (conn.execute(
        "SELECT COALESCE(MAX(failed),0) AS f FROM evaluation_rounds WHERE task_id=?",
        (tid,)).fetchone() or {"f": 0})["f"]
    conn.close()
    ws = tmp_dir / "workspaces" / f"task_{tid}"
    gens = sorted(p.name for p in ws.glob("test_gen*.py"))
    return b_map, failed_n, gens, ws


def replay(fixed_src: Path, gen_files: list[str], ws: Path, img: str):
    fdir = Path(tempfile.mkdtemp(prefix=f"bip_reverify_"))
    shutil.copytree(fixed_src, fdir, dirs_exist_ok=True)
    for g in gen_files:
        shutil.copy(ws / g, fdir / g)
    # 依赖对齐（2026-11-23 三连根因第三层）：真格容器由平台 install_deps 装项目依赖，
    # replay 侧裸 PYTHONPATH——tqdm 纯净无依赖活了下来，cookiecutter（binaryornot）
    # /sanic 一炸 collection。修=replay 前 pip install .（-u root：docker cp 落盘
    # 属 root，tester 无权写 egg-info——maven 重放同款坑）
    cmd = ["docker", "run", "--rm", "-u", "root",
           "-v", f"{fdir.as_posix()}:/work", "-w", "/work",
           "-e", "PYTHONPATH=/work", img, "sh", "-c",
           "python -m pip install -q . 2>&1 | tail -2; "
           "python -m pytest -q -o junit_family=xunit1 "
           f"--junitxml=/work/replay_junit.xml {' '.join(gen_files)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=420)
        rc, tail = r.returncode, (r.stdout or "")[-200:]
    except subprocess.TimeoutExpired:
        rc, tail = -9, "replay 超时"
    f_map = {}
    xml = fdir / "replay_junit.xml"
    if xml.exists():
        for tc in ET.parse(xml).iter("testcase"):
            cls = tc.get("classname") or ""
            nid = f"{cls}::{tc.get('name')}" if cls else (tc.get("name") or "")
            bad = tc.find("failure") is not None or tc.find("error") is not None
            f_map[_norm(nid)] = "failed" if bad else "passed"
    shutil.rmtree(fdir, ignore_errors=True)
    return rc, tail, f_map


def main():
    rows = json.loads(OUT.read_text(encoding="utf-8"))
    for name, repo, bug in CELLS:
        tmp = find_tmp(name)
        fixed = STG / f"bugsinpy-{repo}-bug{bug}" / "fixed"
        if not tmp or not fixed.exists():
            print(f"[skip] {name}: tmp={tmp} fixed={fixed.exists()}")
            continue
        b_map, failed_n, gens, ws = buggy_map(tmp)
        if not gens:
            print(f"[skip] {name}: 无生成文件")
            continue
        rc, tail, f_map = replay(fixed, gens, ws, "mta-test:py38")
        fp = sum(1 for n, bs in b_map.items()
                 if f_map.get(n) == "passed" and bs in ("failed", "error"))
        ff = sum(1 for n, bs in b_map.items()
                 if f_map.get(n) == "failed" and bs in ("failed", "error"))
        pf = sum(1 for n, bs in b_map.items()
                 if f_map.get(n) == "failed" and bs == "passed")
        det = failed_n > 0 and fp >= 1
        both_hang = (rc == -9)
        grade = ("detected" if det else
                 "not_detected" if both_hang else
                 "not_detected" if gens else "no_tests")
        print(f"[reverify] {name}: rc={rc} fp={fp} ff={ff} pf={pf} "
              f"f_map={len(f_map)} → {grade} | tail={tail[-80:]!r}")
        for r in rows:
            if r.get("name") == name:
                r["replay"] = {"n_files": len(gens), "fixed_pass": rc == 0,
                               "detail": f"reverify(py38): exit={rc} tail={tail[-160:]!r}",
                               "fail_to_pass": fp, "fail_to_fail": ff,
                               "pass_to_fail": pf}
                r["failed_in_buggy"] = failed_n
                r["grade"] = grade
                r["grade_note"] = ("2026-11-23 replay 镜像重验：原 replay 硬编码 py312 "
                                   "致 collection error → fp=0 误判；同格 py38 重跑")
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写回 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
