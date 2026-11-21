# -*- coding: utf-8 -*-
"""E10 Wave 2 v2 驱动器（六语言 50×6 = 300 格；2026-11-21 重建，用户拍板）。

v2 变更（对 v1 的差异，本轮实锤驱动）：
- 六语言均分 50×6：用户 2026-11-21 拍板"链路语言无关，不必只测一种语言"。
  格型两分（build_wave2_v2_manifest.py 冻结的 wave2_cells_v2.json，300 specs，
  条件内嵌——v2 不再依赖 E9 留档恢复）：
  * K 格（KB 反哺收敛）：真值基线 × 3 轮独立采样，TRAPKB_V2_ENABLED=True +
    语言 KB 文件（trapkb_v2_{lang}.json，经 flags 通道覆盖 TRAPKB_V2_PATH——
    全局单文件会把 python 失败模式噪声注入其他语言）；对照=基线格（零成本）。
  * D 格（生成分布）：同需求独立采样 1 轮，TRAPKB 关（纯 A1 语义不污染分布）。
  * java 无检出真值基线（全语言 0 条，spring 系失败=已知依赖边界）→ 50 格全 D，
    如实标注；php 仅 4 基线 → K 12 + D 38。每语言 50 格足额。
- ARMED 门：点火令 = E10/wave2_ARMED 文件存在。定时任务每日 18:05 盲跑本驱动器，
  无 ARMED 文件即空转退出——v1 留档 839 条 no-result-mark 错误（旧版驱动器/worker
  不兼容时代产物）的教训：无门 = 定时器反复点火烧垃圾。
- v1 的 300 条 queued 孤儿行（C2 修复前固定 corpus-003/A1 失配批次）已删除，
  库备份 data/test_agent.db.bak_pre_wave2_rebuild_20261121。
- 断点续跑 / 高峰门（复用 run_e9_full 单一真源）/ 402 熔断停波 / wave.lock 互斥 /
  C1 首格停波（先跑 1 格核验再放量）/ 每格即落盘 / 口径闸（每格前复位 profile）——
  全部保留 v1 语义。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
E10 = ROOT / "data" / "reports" / "e10"
OUT = E10 / "wave2_results_v2.json"
RESULT_MARK = "@@RESULT@@"   # E9 worker 留档标记（勿改）
LOCK = ROOT / ".autonomous-loop" / "e9e10_daily" / "wave.lock"  # 与 E9/E10 驱动器同锁互斥
MANIFEST = E10 / "wave2_cells_v2.json"  # v2 冻结清单（300 specs，唯一真源）
ARMED = E10 / "wave2_ARMED"             # 点火令文件（用户令"点火"时创建）

# C1（v1 语义保留）：非终态格默认跳过重试（按任务账对账后人工清行才可重跑——防重复计费）
SETTLED_SKIP = ("done", "error", "timeout", "driver_error",
                "budget_exhausted", "condition_lost")


def load_e9_gate():
    """高峰门单一真源：直接加载 run_e9_full 的窗口函数（勿复制实现）。"""
    spec = importlib.util.spec_from_file_location(
        "run_e9_full_gate", ROOT / "data" / "reports" / "e9_full" / "run_e9_full.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_manifest() -> list[dict]:
    """载入 v2 冻结清单。缺失即 Fatal（清单由 build_wave2_v2_manifest.py 一次性
    生成落盘后不再重算——防采样漂移，v1 教训）。"""
    if not MANIFEST.exists():
        print(f"[fatal] 清单缺失：{MANIFEST}——先跑 build_wave2_v2_manifest.py",
              flush=True)
        raise SystemExit(3)
    specs = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert len(specs) == 300, f"清单格数异常: {len(specs)}"
    return specs


def spec_params(spec: dict) -> dict:
    """spec → worker params。条件全部内嵌自清单（不再查 E9 留档）。"""
    flags = {"TRAPKB_V2_ENABLED": bool(spec.get("trapkb"))}
    if spec.get("kb_path"):
        flags["TRAPKB_V2_PATH"] = str(ROOT / spec["kb_path"])
    return {
        "arm": "A1", "corpus": spec["corpus"],
        "n": 2000 + spec["seq"], "combo_idx": 200 + spec["seq"],
        "flags": flags,
        "requirement": spec["requirement"],   # worker 用显式需求（demo 分支无 REQUIREMENT.md 依赖）
        "label": spec["label"],
        "audit": {k: spec[k] for k in
                  ("key", "seq", "lang", "ctype", "anchor", "baseline_tid",
                   "round") if k in spec},
    }


def run_cell(spec: dict) -> dict:
    """跑一格（独立子进程 worker，复用 E9 run_worker 的 demo 分支与 flags 通道）。"""
    ppath = E10 / "wave2_p.json"
    ppath.write_text(json.dumps(spec_params(spec), ensure_ascii=False),
                     encoding="utf-8")
    t0 = time.time()
    rec: dict = {"key": spec["key"]}
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "data" / "reports" / "e9_full" / "run_e9_full.py"),
             "--worker", str(ppath)],
            capture_output=True,
            encoding="utf-8", errors="replace",   # C1：GBK UnicodeDecodeError 根修
            timeout=45 * 60, cwd=str(ROOT))
        out = r.stdout or ""
        line = next((ln for ln in out.splitlines()
                     if ln.startswith(RESULT_MARK)), None)
        if line:
            rec.update(json.loads(line[len(RESULT_MARK):]))
        else:
            rec.update({"status": "error",
                        "error": (r.stderr or "no result mark")[-300:],
                        "returncode": r.returncode,
                        "reconcile_hint": "留档缺失≠未跑：按 E9 worker 日志/llm_calls "
                                          "对账后清行才可重试（C1 防重复计费）"})
    except subprocess.TimeoutExpired as e:
        rec.update({"status": "timeout", "error": str(e)[:200]})
    except Exception as e:  # noqa: BLE001
        rec.update({"status": "driver_error", "error": f"{type(e).__name__}: {e}"})
    rec["elapsed_s"] = int(time.time() - t0)
    return rec


def main() -> int:
    # ARMED 门：定时器盲跑防线（用户令"点火"= 创建 wave2_ARMED 文件）
    if not ARMED.exists():
        print(f"[armed] 未点火（无 {ARMED.name}）——空转退出。"
              f"点火 = 创建该文件后重跑本驱动器。", flush=True)
        return 0
    if LOCK.exists():
        print(f"[lock] 已有波次锁：{LOCK.read_text(encoding='utf-8').strip()} → 不启动",
              flush=True)
        return 2
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(f"pid={os.getpid()} at={time.strftime('%F %T')} arm=E10WAVE2V2",
                    encoding="utf-8")
    try:
        return _run_wave()
    finally:
        LOCK.unlink(missing_ok=True)


def _run_wave() -> int:
    gate = load_e9_gate()
    specs = load_manifest()
    print(f"Wave2 v2 六语言波: {len(specs)} 格"
          f"（K {sum(1 for s in specs if s['ctype']=='kb')}/"
          f"D {sum(1 for s in specs if s['ctype']=='dist')}）", flush=True)
    # 断点续跑：已留档终态的 key 跳过，不重跑不重复计费
    results: list[dict] = []
    if OUT.exists():
        try:
            results = json.loads(OUT.read_text(encoding="utf-8"))
            print(f"[resume] 载入已留档 {len(results)} 格", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[resume] 留档读取失败（备份后重起）：{e}", flush=True)
            return 3
    settled = {r.get("key") for r in results if r.get("status") in SETTLED_SKIP}
    done_n = sum(1 for r in results if r.get("status") == "done")
    first_cell_ok = False   # C1：首格停波——首个实跑格非终态即停（先核验再放量）
    t0, stop_reason = time.time(), None
    for i, spec in enumerate(specs):
        if stop_reason:
            break
        if spec["key"] in settled:
            continue
        # 高峰门（用户硬规则 2026-11-11）：窗口关闭即停波；00:00–00:30 空隙等开窗
        if not gate.in_offpeak_window():
            m = time.localtime().tm_hour * 60 + time.localtime().tm_min
            if m < 30:  # 窗前空隙：等到 00:31 续跑（晚窗连次日晨窗的中缝）
                wait = (30 - m) * 60 + 60
                print(f"[gate] 00:00–00:30 空隙，等待 {wait // 60} min 后续跑",
                      flush=True)
                time.sleep(wait)
            else:
                stop_reason = "offpeak_window_closed"
                print(f"[stop] {stop_reason}（已留档 {len(results)} 格，"
                      f"余量下个低价窗续跑）", flush=True)
                break
        left = gate.minutes_to_window_close()
        if left < 10:  # 窗口尾不留长格（单格 ~3 min，10 min 安全余量）
            stop_reason = f"window_left_{left:.0f}min_lt_10min"
            print(f"[stop] {stop_reason}（已留档 {len(results)} 格）", flush=True)
            break
        # 口径闸：每格前兜底复位档位，防其他会话中途切档污染口径
        gate.restore_profile_default()
        rec = run_cell(spec)
        results.append(rec)
        if rec.get("status") == "done":
            done_n += 1
            first_cell_ok = True
        # 每格即落盘（断点可查）
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"[cell] {i+1}/{len(specs)} {spec['key']} {spec['lang']}/{spec['ctype']} "
              f"status={rec.get('status')} spent={rec.get('tokens_spent')} "
              f"elapsed={int(time.time() - t0)}s", flush=True)
        # C1 首格停波：首个实跑格非终态即停——先跑 1 格核验再放量
        if (not first_cell_ok
                and rec.get("status") in ("error", "timeout", "driver_error")):
            stop_reason = f"first_cell_{rec.get('status')}"
            print(f"[stop] {stop_reason}——首格核验失败即停波（C1：先核验再放量，"
                  f"留档 {len(results)} 格；对账后清行可续跑）", flush=True)
            break
        if rec.get("status") == "budget_exhausted":
            stop_reason = "budget_exhausted(402)"
            print(f"[stop] {stop_reason}（402 熔断停波留档，余额恢复后续跑）",
                  flush=True)
            break
    if stop_reason is None:
        stop_reason = "plan_exhausted"
    print(f"\n收官: done {done_n}/{len(results)}，stop={stop_reason}，"
          f"本波耗时 {int((time.time()-t0)/60)} 分钟", flush=True)
    print(f"落盘: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
