"""三方权威基准 pilot（2026-11-22 用户令"拿三个最值得的并进行测试"）。

三选：BugsInPy(tqdm bug9，ESEC/FSE 2020)/HumanEvalFix python(BigCode 2023)/
QuixBugs(Lin et al. 2017)。每方 2 item，共 6 真格（≈¥0.3）。

判据（fail-to-pass，SWT-bench 口径）：
- P0 答案键预检（零 LLM）：官方/差分证据 buggy 必败、fixed 必过（不过不进真格）；
- P1 平台真格：MTA 对 buggy 版生成+执行（真实失败=证人）；
- P2 复演：生成用例拷进 fixed 版复跑——buggy 败 && fixed 过 = detected；
  fixed 也败=脚手架破碎（不算检出）；buggy 全过=未检出。

用法：python run_bench3.py --precheck | --run
     worker 模式（父进程自动调）：python run_bench3.py --cell NAME --cell-out P.json

并发隔离（2026-11-22 线程版翻车修复，E9 同款）：每格一个 worker 子进程——
run_cells_serial 改写 config.DATA_DIR/DB_PATH 只在子进程内存生效；旧线程版
6 线程共享全局 config.DB_PATH 互相覆盖 → database is locked ×4 +
UNIQUE users.username ×3，进程边界从结构上消除此类踩踏。
结果合并写（索引覆盖事故教训）：新条目并入 bench3_results.json 既有终态
真账（driver_error 不保留=下次断点自动重试），pilot 真账不再被覆盖丢失。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
STG = ROOT / "data" / "benchmarks" / "staging"
OUT = Path(__file__).resolve().parent / "bench3_results.json"
PY = sys.executable


def _run(cmd, cwd=None, env_extra=None, timeout=180):
    import os
    env = {**os.environ, **(env_extra or {})}
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, check=False)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------- staging ----------

def stage_all():
    items = []

    # ① BugsInPy tqdm bug9（官方失败用例=fixed 的 test_si_format）
    td = STG / "bugsinpy-tqdm-bug9"
    if not (td / "buggy" / "tqdm" / "__init__.py").exists():
        (td / "buggy").mkdir(parents=True, exist_ok=True)
        (td / "fixed").mkdir(parents=True, exist_ok=True)
        for side, tar in (("buggy", "tqdm-buggy.tar.gz"), ("fixed", "tqdm-fixed.tar.gz")):
            subprocess.run(["tar", "-xzf", str(ROOT / "data/benchmarks/repos" / tar),
                            "-C", str(td / side), "--strip-components=1"],
                           check=True, capture_output=True)
    items.append({
        "bench": "BugsInPy(ESEC/FSE'20)", "name": "tqdm-bug9",
        "ws_buggy": str(td / "buggy"), "ws_fixed": str(td / "fixed"),
        "requirement": "测试 tqdm 进度条格式化函数 format_meter 的行为（速率/剩余时间的 SI 单位格式化与边界）",
        "precheck": {"kind": "behavioral-diff",
                     # 999.95 边界差分（官方 bug 真值：buggy 阈值<1000 vs fixed<999.95，
                     # 999951/999999 → buggy '1000K' vs fixed '1.00M'——2026-11-22 容器外
                     # python -S 干净实证；宿主 anaconda tqdm 会遮蔽，必须 -S 隔离）
                     "cmd": [PY, "-S", "-c",
                             "import sys\n"
                             "def fs(side, v):\n"
                             "    for m in list(sys.modules):\n"
                             "        if m.startswith('tqdm'): del sys.modules[m]\n"
                             "    sys.path.insert(0, side)\n"
                             "    from tqdm._tqdm import format_sizeof\n"
                             "    r = format_sizeof(v)\n"
                             "    sys.path.pop(0)\n"
                             "    return r\n"
                             "d = [fs('buggy', v) != fs('fixed', v) for v in (999951, 999999)]\n"
                             "print('DIFF-OK' if all(d) else 'DIFF-MISS')\n"
                             "sys.exit(0 if all(d) else 1)"],
                     "env": {}},
    })

    # ①' BugsInPy 扩量（任务1 2026-11-22 批量注册器）：读 healthcards 目录，
    # P0 预检 ok 的 manifest 自动注册（buggy/fixed 已在 staging；requirement 从
    # manifest 读）。py312 直跑 2019 生态通过率 1/21（版本地狱——请示卡待拍板
    # py3.8 基座路线）；py38 到位后 P0 重跑→健康格自动进本清单（零代码改动）。
    import json as _json
    hc_dir = ROOT / "data/benchmarks/staging/healthcards"
    for hc in sorted(hc_dir.glob("*.json")) if hc_dir.exists() else []:
        card = _json.loads(hc.read_text(encoding="utf-8"))
        if card.get("status") != "ok" or card["id"] == "py-tqdm-bug9":
            continue   # tqdm-bug9 已在 ① 硬编码（behavioral-diff 预检更严）
        mid = card["id"]
        repo = mid[3:].rsplit("-bug", 1)[0]
        sd = STG / f"bugsinpy-{repo}-bug{mid.rsplit('-bug', 1)[1]}"
        if not (sd / "buggy").exists():
            continue
        man = _json.loads((ROOT / "data/benchmarks/manifests" / f"{mid}.json")
                          .read_text(encoding="utf-8"))
        items.append({
            "bench": "BugsInPy(ESEC/FSE'20)", "name": mid[3:],
            "ws_buggy": str(sd / "buggy"), "ws_fixed": str(sd / "fixed"),
            "requirement": (f"测试 {repo} 的行为，重点覆盖官方失败用例所在模块"
                            f"（{card.get('test_file', '')}）的边界与异常路径"),
            # 执行期预检=轻探活（真 P0 已在 healthcard：py38 容器 buggy 必败+fixed 必过）。
            # kind=behavioral-diff 探 DIFF-OK——不能用 kind=official（其语义=buggy 侧
            # 必败，探活命令 buggy/fixed 都成功会被判"预检不过"→全格过滤空集停波，
            # 2026-11-23 凌晨首启实锤）
            "precheck": {"kind": "behavioral-diff",
                         "cmd": [PY, "-S", "-c", "print('DIFF-OK')"],
                         "env": {}},
            # 请示卡 A（2026-11-23）：healthcard 以 py38 基座验证——真格同镜像
            # （2019 生态原生环境；tqdm-bug9 硬编码格保持 py312 与 r1-r3 可比）
            "image_override": "mta-test:py38",
        })

    # ② HumanEvalFix python（前 2 个小函数；答案键=官方 check）
    import pandas as pd
    df = pd.read_parquet(ROOT / "data/benchmarks/humanevalfix/python.parquet")
    for _, row in df.head(30).iterrows():   # 中档：python 30 题（用户拍板直接上中档）
        tid = row["task_id"].replace("/", "_")
        d = STG / f"hef-{tid}"
        ep = row["entry_point"]
        # 幂等守卫：并发 worker 各自 stage_all()，不得重写已布考场
        # （write_text 非原子——P2 复演窗口可能读到半文件）；守卫只跳写盘，
        # 条目注册必须照常（2026-11-22 干跑抓出：continue 连 append 一起跳=整格消失）
        if not (d / "buggy" / f"mod_{ep}.py").exists():
            for side, body in (("buggy", row["buggy_solution"]),
                               ("fixed", row["canonical_solution"])):
                (d / side).mkdir(parents=True, exist_ok=True)
                mod = (f"{row['import'] or ''}\n{row['declaration']}\n{body}\n")
                (d / side / f"mod_{ep}.py").write_text(mod, encoding="utf-8")
                chk = (f"from mod_{ep} import {ep}\n"
                       f"{row['test']}\ncheck({ep})\nprint('OFFICIAL-OK')\n")
                (d / side / "test_official.py").write_text(chk, encoding="utf-8")
        items.append({
            "bench": "HumanEvalFix(BigCode'23)", "name": tid,
            "ws_buggy": str(d / "buggy"), "ws_fixed": str(d / "fixed"),
            "requirement": f"测试函数 {row['entry_point']} 的行为。需求描述：{row['docstring']}",
            "precheck": {"kind": "official",
                         "cmd": [PY, "test_official.py"], "env": {}},
        })

    # ③ QuixBugs（无本地依赖的独立程序取 2 个；答案键=benchmark 官方单行修复 diff）
    qd = ROOT / "data/benchmarks/quixbugs/QuixBugs-master"
    import re as _re
    picked = []
    for f in sorted((qd / "python_programs").glob("*.py")):
        if f.stem.startswith("_") or f.name.endswith("_test.py"):
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        if "import node" in txt or "from ." in txt or f.stem == "node":
            continue
        if not (qd / "correct_python_programs" / f.name).exists():
            continue
        doc = txt.split('"""')[1] if '"""' in txt else f.stem
        picked.append((f, (qd / "correct_python_programs" / f.name), doc))
    for f, fixed_f, doc in picked:
        d = STG / f"quix-{f.stem}"
        if not (d / "buggy" / f.name).exists():   # 幂等守卫（只跳写盘，条目照常注册）
            (d / "buggy").mkdir(parents=True, exist_ok=True)
            (d / "fixed").mkdir(parents=True, exist_ok=True)
            shutil.copy(f, d / "buggy" / f.name)
            shutil.copy(fixed_f, d / "fixed" / f.name)
        items.append({
            "bench": "QuixBugs(Lin'17)", "name": f.stem,
            "ws_buggy": str(d / "buggy"), "ws_fixed": str(d / "fixed"),
            "requirement": f"测试函数 {f.stem} 的行为。程序说明：{doc.strip()[:400]}",
            "precheck": {"kind": "diff-structural", "cmd": None, "env": {}},
        })
    return items


# ---------- P0 答案键预检（零 LLM） ----------

def precheck(items):
    results = []
    for it in items:
        rec = {"name": it["name"], "bench": it["bench"], "kind": it["precheck"]["kind"]}
        if it["precheck"]["kind"] == "official":
            try:
                cb, ob = _run(it["precheck"]["cmd"], cwd=it["ws_buggy"],
                              env_extra=it["precheck"]["env"], timeout=60)
            except subprocess.TimeoutExpired:
                cb, ob = -9, ""       # buggy 死循环=真失败证据
            try:
                cf, of = _run(it["precheck"]["cmd"], cwd=it["ws_fixed"],
                              env_extra=it["precheck"]["env"], timeout=60)
            except subprocess.TimeoutExpired:
                cf, of = -9, ""      # fixed 也死循环=考卷本身坏 → 预检 FAIL
            rec["buggy_fail"] = cb != 0
            rec["fixed_pass"] = (cf == 0 and "OFFICIAL-OK" in of) or cf == 0
            rec["ok"] = rec["buggy_fail"] and rec["fixed_pass"]
            rec["detail"] = f"buggy_exit={cb} fixed_exit={cf} fixed_tail={of[-120:]!r}"
        elif it["precheck"]["kind"] == "behavioral-diff":
            # 命令内自带双版本差分（cwd=staging 根，脚本内切 buggy/fixed 子目录）
            c, o = _run(it["precheck"]["cmd"], cwd=str(Path(it["ws_buggy"]).parent),
                        env_extra=it["precheck"]["env"])
            rec["ok"] = c == 0 and "DIFF-OK" in o
            rec["detail"] = f"exit={c} out={o.strip()[-80:]!r}"
        else:
            # replay 残留防御（2026-11-23 凌晨实锤：wrap 的 fixed 目录有 test_gen_r1.py
            # 残留→glob 拿错"fixed 源码"）；QuixBugs 源文件=小写开头，排除生成物
            def _src_py(ws):
                cands = sorted(p for p in Path(ws).glob("*.py")
                               if not p.name.startswith("test_gen"))
                return cands[0]
            bf = _src_py(it["ws_buggy"])
            ff = _src_py(it["ws_fixed"])
            # GBK 解码坑（同夜实锤：wrap diff 输出含 UTF-8 中文 docstring，
            # text=True 默认 GBK→readerthread UnicodeDecodeError→stdout=None）
            diff = subprocess.run(["diff", str(bf), str(ff)], capture_output=True,
                                  text=True, encoding="utf-8",
                                  errors="replace").stdout
            rec["ok"] = bool(diff.strip())
            rec["detail"] = f"官方单行修复 diff 存在（benchmark 保证 bug 真实）：" \
                            f"{[l for l in diff.splitlines() if l.startswith('<')][:1]}"
        print(("[PASS] " if rec["ok"] else "[FAIL] ") + f"P0 {it['name']}: {rec['detail'][:150]}")
        results.append(rec)
    return results


# ---------- P1+P2 平台真格+复演 ----------

def run_cells(items, skip_c1: bool = False):
    """C1 放量纪律内嵌（2026-11-22 中档）：前 5 格（pilot 组）先跑核验——
    detected ≥2 且脚手架破碎 ≤1 才放量其余格；不合格停波（防批量废案烧钱，
    批 3 白烧 205k 教训）。pilot 也走 worker 子进程（串行）=worker 链路在
    放量前先自证 5 格；并发=每格一个子进程（E9 同款）。
    2026-11-22 线程版翻车教训：6 线程共享全局 config.DB_PATH 互相覆盖
    （database is locked ×4 + UNIQUE users.username ×3）——进程边界天然
    隔离，子进程内改写 config 出不了进程。

    skip_c1（2026-11-23 探针实锤）：C1 是"管线健康才放量"的闸，判据 detected≥2——
    但治疗臂探针恰恰跑在【基线 0 检出】的格上，低检出是预期读数而非管线故障。
    两次探针都被 C1 在 pilot 处截停（0/5）只拿到 n=5。--arm-tag 存在时自动跳过
    C1 的 detected 判据（臂实验必须拿全量读数），其余护栏（402 熔断/driver_error
    >8 停车/worker 1200s 超时）全部保留——那些才是护钱的。"""
    results = []
    n_pilot = min(5, len(items))
    pilot = items[:n_pilot]
    rest = items[n_pilot:]
    if skip_c1:
        # 臂实验：pilot 组仍串行先跑（首格自证链路），但不以检出率截停
        results.extend(run_cells_workers(pilot, max_workers=1))
        print(f"[C1-skip] 臂实验模式：pilot {n_pilot} 格已跑，不以检出率截停", flush=True)
        print(f"[放量] 臂余 {len(rest)} 格 × 6 worker 子进程", flush=True)
        results.extend(run_cells_workers(rest))
        return results
    pilot_res = run_cells_workers(pilot, max_workers=1)
    results.extend(pilot_res)
    n_det = sum(1 for r in pilot_res if r["grade"] == "detected")
    n_broken = sum(1 for r in pilot_res if r["grade"] == "scaffold_broken")
    print(f"[C1] pilot 核验: detected {n_det}/{n_pilot} 破碎 {n_broken}", flush=True)
    if n_det < 2 or n_broken > 1:
        print(f"[stop] pilot 判定率不合格（detected {n_det}<2 或破碎 {n_broken}>1）"
              "——停波不放量（转换形态先修，不烧钱）", flush=True)
        return results
    print(f"[放量] 中档余 {len(rest)} 格 × 6 worker 子进程", flush=True)
    results.extend(run_cells_workers(rest))
    return results


def run_cells_workers(items, max_workers=6):
    """每格一个 worker 子进程（python run_bench3.py --cell NAME --cell-out P）。
    父进程线程只做 spawn+收割，不 touch config；子进程内 run_cells_serial
    改写 config.DATA_DIR/DB_PATH 出不了自己的进程。异常格>8 停车并杀活 worker。"""
    ctl = Path(tempfile.mkdtemp(prefix="bench3_ctl_"))
    handles = {}
    results, stop = [], False

    def _launch(it):
        out_p = ctl / f"{it['name']}.json"
        log_p = ctl / f"{it['name']}.log"
        cmd = [PY, str(Path(__file__).resolve()), "--cell", it["name"],
               "--cell-out", str(out_p)]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8",
               "BENCH3_ROUND_TIMEOUT_S": "90"}
        with open(log_p, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=lf,
                                 stderr=subprocess.STDOUT, env=env)
            handles[it["name"]] = p
            try:
                rc = p.wait(timeout=1200)
            except subprocess.TimeoutExpired:
                p.kill()
                return [{"name": it["name"], "grade": "driver_error",
                         "error": "worker 超时 1200s 被杀"}]
        if out_p.exists():
            try:
                return json.loads(out_p.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                return [{"name": it["name"], "grade": "driver_error",
                         "error": f"worker 结果不可读: {e}"}]
        tail = ""
        try:
            tail = log_p.read_text(encoding="utf-8", errors="replace")[-300:]
        except Exception:   # noqa: BLE001
            pass
        return [{"name": it["name"], "grade": "driver_error",
                 "error": f"worker exit={rc} 无结果 JSON：{tail}"}]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_launch, it): it for it in items}
        pending = list(futs)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                it = futs[f]
                try:
                    res = f.result()
                except Exception as e:  # noqa: BLE001
                    res = [{"name": it.get("name"), "grade": "driver_error",
                            "error": f"{type(e).__name__}: {str(e)[:160]}"}]
                results.extend(res)
                head = res[0] if res else {}
                print(f"[worker done] {head.get('name')}: {head.get('grade')} "
                      f"fp={((head.get('replay') or {}).get('fail_to_pass'))}", flush=True)
                # 护栏口径修正（2026-11-23 凌晨实锤）：异常格=driver_error 类基础设施错；
                # scaffold_broken 是终态判定（per-test 5s 下挂死族从证据蒸发变破碎，
                # 9 个破碎触发误停杀掉 12 格 pending）——从异常计数剔除
                n_det = sum(1 for r in results if r["grade"] == "detected")
                n_all = len(results)
                n_term_ok = sum(1 for r in results if r["grade"] in
                                ("not_detected", "scaffold_broken", "no_tests"))
                if n_all - n_det - n_term_ok > 8:
                    print("[stop] 异常格(driver_error) >8——停车对账（杀活 worker）",
                          flush=True)
                    stop = True
            if stop:
                for f2 in pending:
                    f2.cancel()
                for p in list(handles.values()):
                    try:
                        p.kill()
                    except Exception:   # noqa: BLE001
                        pass
                break
    return results


def run_cells_serial(items):
    from test_agent import config
    import secrets
    results = []
    for it in items:
        tmp = Path(tempfile.mkdtemp(prefix=f"bench3_{it['name']}_"))
        config.DATA_DIR = tmp
        config.DB_PATH = tmp / "t.db"
        # 死循环陷阱格的时钟经济（2026-11-22 第三轮实锤）：单轮执行超时默认 600s，
        # 挂死用例每轮烧满 → repair 再生成再挂 → worker 1200s 封顶杀格=检出信号丢失。
        # 小程序基准 90s 对非挂用例 50 倍余量；挂死照旧被击杀留证人（0-C 语义不变）。
        config.DEFAULT_JOB_TIMEOUT_S = int(os.environ.get("BENCH3_ROUND_TIMEOUT_S", "600"))
        from test_agent.store import db as db_mod
        db_mod.init_db()
        conn = db_mod.get_conn()
        uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                           "VALUES('b3','x','developer')").lastrowid
        pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                           (uid, it["ws_buggy"], it["name"])).lastrowid
        # 唯一 9 位 task_id：容器名 mta-run-{task_id}/快照路径随 task_id 全部唯一。
        # 2026-11-22 实锤（修复前全格 task_id=1 → 并发 worker 共享 mta-run-1）：
        # ensure_container(fresh=True) 会销毁同名旧容器——B 删了 A 正在用的容器，
        # A 随后 containers.get("mta-run-1") 拿到 B 的容器、在 B 的工作区执行、
        # 把 B 的用例结果记进 A 的账（Python_13/16/17/18 串账取证），其余格
        # 409 Conflict 直接炸。tmp 库自增从 1 起 = 名字必然撞车，随机 9 位根治。
        tid = 900_000_000 + secrets.randbelow(99_999_998)
        conn.execute(
            "INSERT INTO test_tasks(id,project_id,user_id,coverage_target,status,"
            "confirmed,requirement_text) VALUES(?,?,?,?, 'ready_for_review',1,?)",
            (tid, pid, uid, 0.7, it["requirement"]))
        conn.commit()
        # 工作区=buggy 目录的平台可写副本（平台会写生成文件）
        ws = tmp / "workspaces" / f"task_{tid}"
        shutil.copytree(it["ws_buggy"], ws)
        t0 = time.time()
        verdict, stop, err = "-", "", ""
        try:
            from test_agent.graph.builder import run_task
            final = run_task(tid)
            verdict, stop = final.get("verdict", "-"), (final.get("stop_reason") or "")[:160]
        except Exception as e:  # noqa: BLE001
            verdict, err = "driver_error", f"{type(e).__name__}: {str(e)[:200]}"
        row = conn.execute("SELECT status, error FROM test_tasks WHERE id=?",
                           (tid,)).fetchone()
        failed_n = (conn.execute(
            "SELECT COALESCE(MAX(failed),0) AS f FROM evaluation_rounds WHERE task_id=?",
            (tid,)).fetchone() or {"f": 0})["f"]
        gen_files = sorted(p.name for p in ws.glob("test_gen*"))
        # P2 复演：生成用例拷进 fixed 版复跑（宿主 python；tqdm 需 PYTHONPATH=. 指向本地包）
        replay = {"n_files": len(gen_files), "fixed_pass": None, "detail": ""}
        replay_ran = False
        if gen_files:
            # 复演目录=每格独立 tmp 副本（2026-11-23 凌晨竞态实锤：__r2/__r3 共享
            # staging fixed 目录+同名 test_gen_r*.py，A worker 跑完 unlink 清理删掉
            # B worker replay 正要用的文件→exit=4 file not found 8 格——串账事故的
            # 表亲：共享可变目录×并发。tmp 副本天然隔离，staging 保持只读纯净）
            fdir = Path(tempfile.mkdtemp(prefix=f"bench3_replay_{it['name'][:24]}_"))
            shutil.copytree(it["ws_fixed"], fdir, dirs_exist_ok=True)
            for g in gen_files:
                shutil.copy(ws / g, fdir / g)
            # P2 复演=容器内执行（2026-11-22 用户纠偏：研究方向=容器隔离沙箱，
            # 宿主 python 与真格环境不同源 → fail-to-pass 判定有环境偏差风险。
            # 与真格同镜像同挂载形态 /work，环境同源可复现。
            # 2026-11-23 修复×2：①镜像跟随格的 image_override（py38 格曾硬编码
            # py312 → collection 炸 → fp=0 误判）②override 格补 pip install .——
            # 真格容器由平台 install_deps 装项目依赖，replay 裸 PYTHONPATH 对
            # cookiecutter（binaryornot）类必炸（reverify 实锤）；-u root 同
            # maven 教训：docker cp 落盘 root 属主）
            replay_img = it.get("image_override") or "mta-test:py312"
            if it.get("image_override"):
                replay_cmd = ["docker", "run", "--rm", "-u", "root",
                              "-v", f"{fdir.as_posix()}:/work", "-w", "/work",
                              "-e", "PYTHONPATH=/work", replay_img, "sh", "-c",
                              "python -m pip install -q . 2>&1 | tail -1; "
                              "python -m pytest -q -o junit_family=xunit1 "
                              f"--junitxml=/work/replay_junit.xml {' '.join(gen_files)}"]
            else:
                replay_cmd = ["docker", "run", "--rm",
                              "-v", f"{fdir.as_posix()}:/work", "-w", "/work",
                              "-e", "PYTHONPATH=/work",
                              replay_img,
                              "python", "-m", "pytest", "-q",
                              "-o", "junit_family=xunit1",
                              "--junitxml=/work/replay_junit.xml", *gen_files]
            try:
                rf, of_ = _run(replay_cmd, timeout=300)
            except subprocess.TimeoutExpired:
                # 第三轮 wrap 实锤：fixed 版也挂死（两版共有非终止边界，类③）——
                # 超时不再炸 worker，如实记 detail 走 scaffold_broken/坏用例口径
                rf, of_ = -9, "replay 超时 300s 被杀（fixed 版也挂死=类③非终止边界）"
            replay["fixed_pass"] = (rf == 0)
            replay["detail"] = f"exit={rf} tail={of_[-160:]!r}"
            # P2 判定升级：逐用例转移矩阵（SWT-bench 级——整文件 exit 口径会把
            # fail-to-pass 真检出误判破碎：pilot 取证 22 条证据被埋）
            import xml.etree.ElementTree as ET

            def _norm(nid: str) -> str:
                n = nid.replace("\\", "/").replace(".py::", "::").replace(".py:", "::")
                return n.strip()

            f_map = {}
            xml = fdir / "replay_junit.xml"
            if xml.exists():
                for tc in ET.parse(xml).iter("testcase"):
                    cls = tc.get("classname") or ""
                    nid = f"{cls}::{tc.get('name')}" if cls else (tc.get("name") or "")
                    failed = tc.find("failure") is not None or tc.find("error") is not None
                    f_map[_norm(nid)] = "failed" if failed else "passed"
            # 判级判别器：复演真跑起来了（junit 有解析结果）≠ 脚手架破碎
            # （第三轮 tqdm 实锤：13 条在 fixed 版正常执行零命中被误标 scaffold_broken）
            replay_ran = bool(f_map)
            b_map = {}
            for r_ in conn.execute(
                    "SELECT nodeid, status FROM test_case_results WHERE task_id=? ORDER BY round_no, id",
                    (tid,)):
                b_map[_norm(r_["nodeid"])] = r_["status"]
            fp = sum(1 for n, bs in b_map.items()
                     if f_map.get(n) == "passed" and bs in ("failed", "error"))
            ff = sum(1 for n, bs in b_map.items()
                     if f_map.get(n) == "failed" and bs in ("failed", "error"))
            pf = sum(1 for n, bs in b_map.items()
                     if f_map.get(n) == "failed" and bs == "passed")
            replay["fail_to_pass"] = fp
            replay["fail_to_fail"] = ff
            replay["pass_to_fail"] = pf
            # tmp 副本整体清理（原 saved/unlink 针对 staging 共享目录——竞态根因，
            # 已改 tmp 副本，无需逐文件回滚；finally 保证异常路径也清）
            try:
                shutil.rmtree(fdir, ignore_errors=True)
            except Exception:   # noqa: BLE001
                pass
        detected = bool(gen_files) and failed_n > 0 and fp >= 1
        task_err_txt = row["error"] or err or ""
        # 类③口径修正（2026-11-23 凌晨）：fixed 版也挂死（replay 超时被杀/exit 137/-9）
        # =两版共挂非终止边界（bench3 第四轮三分类的类③），不是脚手架破碎——
        # per-test 5s 下挂死信号能进 junit（failed>0）但 fixed replay 超时 → 首轮
        # repeat 把这类误判 scaffold_broken（r1 是 not_detected，口径漂移 9 格）。
        rep_detail = (replay.get("detail") or "")
        both_hang = (not replay["fixed_pass"]) and (
            "超时" in rep_detail or "exit=137" in rep_detail or "exit=-9" in rep_detail)
        # driver_error 永远非终态（可重试）；402/余额不足=环境问题不算 agent 无产出
        budget_hit = any(k in task_err_txt for k in ("BudgetExhausted", "402"))
        verdict_cls = ("driver_error" if verdict == "driver_error" or budget_hit else
                       "detected" if detected else
                       "not_detected" if (both_hang and gen_files) else
                       "scaffold_broken" if gen_files and failed_n > 0 and fp == 0
                       and not replay_ran else
                       "not_detected" if gen_files else "no_tests")
        rec = {"bench": it["bench"], "name": it["name"], "task_id": tid,
               "status": row["status"], "verdict": verdict, "stop_reason": stop,
               "task_error": (row["error"] or err or "")[:200],
               "failed_in_buggy": failed_n, "gen_files": gen_files,
               "replay": replay, "grade": verdict_cls,
               "duration_s": round(time.time() - t0, 1)}
        if it.get("arm_tag"):
            rec["arm"] = it["arm_tag"]          # 血统字段：不靠名字推断臂
        print(f"[cell] {it['name']}: verdict={verdict} failed={failed_n} "
              f"fail-to-pass={replay.get('fail_to_pass')} → {verdict_cls} "
              f"({rec['duration_s']}s)", flush=True)
        results.append(rec)
        try:
            conn.close()
        except Exception:   # noqa: BLE001
            pass
    return results


def strip_cell_decorations(name: str) -> tuple[str, str | None, int | None]:
    """单元格全名 → (原格名, 臂标签, 重复序号)。三种装饰的剥离唯一真源：
      base            → (base, None, None)
      base__rN        → (base, None, N)
      base@arm        → (base, arm, None)
      base@arm__rN    → (base, arm, N)
    worker 侧用它把带装饰的名映射回 stage_all 的原格；main 侧 arm_decorate
    是它的逆运算——两者必须成对（tests/test_arm_namespace.py 锁）。"""
    rep = None
    core = name
    if "__r" in core:
        core, tail = core.rsplit("__r", 1)
        if tail.isdigit():
            rep = int(tail)
        else:                               # 不是重复后缀（名字本身含 __r）
            core = name
    arm = None
    if "@" in core:
        core, arm = core.split("@", 1)
    return core, arm, rep


def run_one_cell_cli(name, out_path):
    """worker 子进程入口：隔离修复的核心——本进程内改写 config 只影响自己。
    装饰名剥壳（2026-11-23 两次根修）：worker 重新 stage_all 只有原名，带
    __rN / @arm 装饰的名会"未知格"秒退——strip_cell_decorations 剥回原格，
    结果 name 写回带装饰全名（staging 共享同一场，独立采样语义不变）。
    image_override（2026-11-23 请示卡 A）：BugsInPy 格走 py38 基座（2019 生态原生
    环境）——必须在 import test_agent.config 前写 env（config 模块加载时求值）；
    worker 子进程内 env 出不了进程，HEF/QuixBugs 格不受影响。"""
    items = stage_all()
    core, arm, _rep = strip_cell_decorations(name)
    match = [i for i in items if i["name"] == core]
    if not match:
        print(f"[worker] 未知格 {name}", flush=True)
        return 2
    match = [dict(match[0], name=name)]
    if arm:
        match[0]["arm_tag"] = arm
    if not match:
        print(f"[worker] 未知格 {name}", flush=True)
        return 2
    if match[0].get("image_override"):
        os.environ["MTA_PY_IMAGE"] = match[0]["image_override"]
        print(f"[worker] {name}: 镜像覆盖 → {match[0]['image_override']}", flush=True)
    res = run_cells_serial([match[0]])
    Path(out_path).write_text(json.dumps(res, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--precheck", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--cell", help="worker 模式：只跑该 name 的格（子进程隔离）")
    ap.add_argument("--cell-out", help="worker 模式：结果 JSON 输出路径")
    ap.add_argument("--repeat", type=int, default=1,
                    help="小件3：重复采样 n（>1 时每格跑 n 次独立采样，格名 __r{i} "
                         "后缀进断点名——r1 用原名；staging 共享）")
    ap.add_argument("--bench", default="",
                    help="只跑 bench 名含该串的格（如 BugsInPy——探针/消融臂过滤）")
    ap.add_argument("--arm-tag", default="",
                    help="实验臂独立名空间（2026-11-23 探针翻车根修）：给了就把【每格】"
                         "重命名为 {name}@{tag}——与基线名彻底隔离。此前的坑：--repeat "
                         "的首样本沿用基线原名，无基线记录的格会以治疗臂旗标跑出"
                         "基线名记录（5 格实锤），有记录的则被断点跳过=治疗臂空跑")
    args = ap.parse_args()
    if args.cell:
        sys.exit(run_one_cell_cli(args.cell, args.cell_out))
    items = stage_all()
    if args.bench:
        items = [i for i in items if args.bench in i["bench"]]
    if args.repeat > 1:
        extra = []
        for it in items:
            for i in range(2, args.repeat + 1):
                extra.append({**it, "name": f"{it['name']}__r{i}"})
        items = items + extra
    if args.arm_tag:
        # 臂标签包住【整格清单含首样本】。命名规范 = base@arm__rN（strip_cell_decorations
        # 的逆运算）：先剥 __rN 再剥 @arm——所以 @arm 必须插在 __rN 【之前】。
        # 首版写成 name@arm（arm 在最外层 → base__rN@arm）与剥壳协议不互逆，
        # tests/test_arm_namespace.py 锁死本顺序。
        def _arm_name(n: str, tag: str) -> str:
            if "__r" in n:
                core, tail = n.rsplit("__r", 1)
                if tail.isdigit():
                    return f"{core}@{tag}__r{tail}"
            return f"{n}@{tag}"
        items = [{**it, "name": _arm_name(it["name"], args.arm_tag),
                  "arm_tag": args.arm_tag} for it in items]
    print(f"staged {len(items)} items:", [i["name"] for i in items])
    if args.precheck:
        pre = precheck(items)
        sys.exit(0 if all(p["ok"] for p in pre) else 1)
    if args.run:
        pre = precheck(items)
        done_names = set()
        rp = Path(__file__).resolve().parent / "bench3_results.json"
        if rp.exists():
            try:
                done_names = {r.get("name") for r in json.loads(rp.read_text(encoding="utf-8"))
                              if r.get("grade") in ("detected", "scaffold_broken",
                                                    "not_detected", "no_tests")}
            except Exception:   # noqa: BLE001
                pass
        ok_items = [it for it, p in zip(items, pre) if p["ok"] and it["name"] not in done_names]
        skipped = len(items) - len(ok_items) - sum(1 for it, p in zip(items, pre) if not p["ok"])
        print(f"预检过 {len(ok_items)}/{len(items)}，断点跳过已终态 {skipped} 格"
              f"（中档剩余 {len(ok_items)} 格，转移矩阵判定版）")
        res = run_cells(ok_items, skip_c1=bool(args.arm_tag))
        # 合并写（索引覆盖事故教训）：只追加/覆盖本跑条目，既有终态真账保留；
        # driver_error 旧条目不保留=下次断点自动重试
        merged = {}
        if rp.exists():
            try:
                for r in json.loads(rp.read_text(encoding="utf-8")):
                    if r.get("grade") in ("detected", "scaffold_broken",
                                          "not_detected", "no_tests"):
                        merged[r["name"]] = r
            except Exception:   # noqa: BLE001
                pass
        for r in res:
            merged[r.get("name")] = r   # 新结果覆盖同名旧条目
        final = list(merged.values())
        OUT.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
        n_det = sum(1 for r in final if r["grade"] == "detected")
        n_term = sum(1 for r in final if r["grade"] != "driver_error")
        print(f"\n终判: detected {n_det}/{n_term}（fail-to-pass 口径，"
              f"driver_error 不进分母）→ {OUT}")
        for r in final:
            print(f"  {r['name']}: {r['grade']}（failed={r.get('failed_in_buggy')} "
                  f"fixed_replay={(r.get('replay') or {}).get('fixed_pass')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
