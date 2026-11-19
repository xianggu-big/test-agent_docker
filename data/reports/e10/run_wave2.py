# -*- coding: utf-8 -*-
"""E10 Wave 2 主力波驱动器（Arm 1 失败知识反哺：100 任务 × 3 轮 = 300 格）。

- 选格：E9 failed 语料任务（GT 已知或证人已冻结）按 id 均匀取 100；
- 每任务 3 轮独立采样（trapkb 注入全程 ON），对照组=E9 原格（零成本）；
- 指标：每轮检出数/覆盖率/通过数 → 收敛曲线（每轮信息增量）；
- 纪律：402 不降级（直接标 budget_exhausted 停波留档）/ 每格 JSON 留档 / 高峰门
  由点火时间控制（18:01 低价窗起跑）。

2026-11-17 续跑链路修复（当日实锤：00:14 点火跑 7 格即断，重跑覆盖已留档、
高峰段无门、402 不停波、未接入 18:05 定时器）——四项补齐，全部为驱动器层：
1. 断点续跑：载入 wave2_results.json 已留档 done 的 (baseline_tid, round) 跳过；
2. 高峰门：复用 run_e9_full.in_offpeak_window/minutes_to_window_close（单一真源，
   用户硬规则 2026-11-11）；00:00–00:30 窗前空隙就等 30 分钟，其余关闭即停波；
3. 402 熔断：首格 budget_exhausted 即停波留档，余额恢复后下个低价窗续跑；
4. 波次锁：复用 E9 的 wave.lock（与 E9/E10 驱动器互斥，严禁并行双波）。
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
E9_FULL = ROOT / "data" / "reports" / "e9_full"
OUT = E10 / "wave2_results.json"
RESULT_MARK = "@@RESULT@@"   # E9 worker 留档标记（勿改——Wave 1 三轮调试教训）
LOCK = ROOT / ".autonomous-loop" / "e9e10_daily" / "wave.lock"  # 与 E9 驱动器同锁互斥
MANIFEST = E10 / "wave2_cells.json"  # 冻结选格清单（一次性落盘后为唯一真源）

# C1/C2（2026-11-20 设计方案，Qoder 体检论断 4）：
# - 首格停波：slot0/r1 非终态即停波（批3 白烧 205k 教训升级：先跑 1 格核验再放量）；
# - error 格默认跳过重试（按任务账对账后人工清行才可重跑——防重复计费）；
# - 条件快照恢复：每格参数从基线任务的 E9 留档恢复（arm/corpus/flags）——冻结任务
#   编号不等于冻结实验条件（修复前固定 corpus-003/A1，基线 #420 是 corpus-001/A1
#   也照跑 corpus-003=对照失配）；条件不可恢复的格标 condition_lost 不跑。
CONDITION_LOST_SKIP = ("done", "error", "timeout", "driver_error",
                       "condition_lost", "budget_exhausted")


def load_baseline_condition(baseline_tid: int) -> dict | None:
    """从 E9 留档恢复基线格的完整实验条件（arm/corpus/flags/combo_idx）。
    扫 data/reports/e9_full/*.json 找 task_id 命中；找不到返回 None（调用方如实标
    condition_lost 不跑）。n 用 wave2 自己的编号空间（2000+seq，防任务标签冲突），
    不从基线恢复。"""
    for fp in sorted(E9_FULL.glob("*.json")):
        if fp.name.startswith(("progress", "run_", "aggregate")):
            continue   # 汇总/进度文件不含单格条件
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict) and d.get("task_id") == baseline_tid:
            return {"arm": d.get("arm"), "corpus": d.get("corpus"),
                    "flags": d.get("flags"), "combo_idx": d.get("combo_idx"),
                    "source": fp.name}
    return None


def load_e9_gate():
    """高峰门单一真源：直接加载 run_e9_full 的窗口函数（勿复制实现）。"""
    spec = importlib.util.spec_from_file_location(
        "run_e9_full_gate", ROOT / "data" / "reports" / "e9_full" / "run_e9_full.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_manifest() -> list[int]:
    """冻结 100 任务选格清单（2026-11-17 实锤：wave2 自产复跑任务标签
    e9a1-c2xx-corpus-003-n2xxx 命中 'e9a%corpus%' 且终态 failed，回流选格池使
    均匀采样 step 随跑随漂——00:14 首波 slot2=428，重启后漂成 429，断点键错位）。
    清单落盘后不再重算；已留档 done 的 (task_slot→tid) 锚定保留，其余槽位按当前
    均匀采样顺序填充并跳过已用 tid（3/100 锚定对均匀性影响可忽略，代码如实注明）。"""
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    done_slots: dict[int, int] = {}
    if OUT.exists():
        try:
            for r in json.loads(OUT.read_text(encoding="utf-8")):
                if r.get("status") == "done" and "task_slot" in r:
                    done_slots[r["task_slot"]] = r["baseline_tid"]
        except Exception:  # noqa: BLE001
            pass
    cand = [c["tid"] for c in pick_cells(100)]
    used = set(done_slots.values())
    slots: dict[int, int] = {}
    ci = 0
    for slot in range(100):
        if slot in done_slots:
            slots[slot] = done_slots[slot]
            continue
        while ci < len(cand) and cand[ci] in used:
            ci += 1
        if ci >= len(cand):
            break
        slots[slot] = cand[ci]
        used.add(cand[ci])
        ci += 1
    manifest = [slots[i] for i in sorted(slots)]
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"[manifest] 冻结选格清单 {len(manifest)} 任务 → {MANIFEST.name}", flush=True)
    return manifest


def pick_cells(n_tasks: int = 100) -> list[dict]:
    import sqlite3
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'test_agent.db').as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT t.id, p.name FROM test_tasks t JOIN projects p ON p.id=t.project_id
        WHERE p.name LIKE 'e9a%corpus%' AND t.status='failed'
        ORDER BY t.id""").fetchall()
    c.close()
    ids = [r["id"] for r in rows]
    # 均匀抽样 100（跨语料覆盖）
    if len(ids) > n_tasks:
        step = len(ids) / n_tasks
        ids = [ids[int(i * step)] for i in range(n_tasks)]
    return [{"tid": tid} for tid in ids]


def run_cell(baseline_tid: int, seq: int, round_no: int,
             condition: dict | None = None) -> dict:
    """跑一格。C2：实验条件从基线快照恢复（arm/corpus/flags），n/combo_idx 用
    wave2 自己的编号空间（防任务标签冲突）；TRAPKB_V2_ENABLED=True 是 Arm 1
    唯一自变量（失败知识反哺 ON），覆盖基线 flags 中的同名键。"""
    rec = {"baseline_tid": baseline_tid, "round": round_no, "seq": seq}
    if not condition:
        # C2：条件不可恢复的格不跑——对照失配的数字不如没有数字（如实留档）
        rec.update({"status": "condition_lost",
                    "error": "基线条件不可恢复（E9 留档无此 task_id）——不跑（C2 条件冻结）"})
        rec["elapsed_s"] = 0
        return rec
    params = {
        "arm": condition["arm"], "corpus": condition["corpus"],
        "n": 2000 + seq, "combo_idx": 200 + seq,
        "flags": {**(condition.get("flags") or {}), "TRAPKB_V2_ENABLED": True},
        "profile": None,
    }
    rec["condition_source"] = condition.get("source")   # C2：条件快照可审计
    ppath = E10 / f"wave2_p.json"
    ppath.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    t0 = time.time()
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
                        # C1：error 格对账提示——worker 可能已建任务烧了 token，
                        # 按库中 llm_calls（task_id 近似段）对账，勿盲目重试
                        "reconcile_hint": "留档缺失≠未跑：按 E9 worker 日志/llm_calls "
                                          "对账后清行才可重试（C1 防重复计费）"})
    except subprocess.TimeoutExpired as e:
        rec.update({"status": "timeout", "error": str(e)[:200]})
    except Exception as e:  # noqa: BLE001
        rec.update({"status": "driver_error", "error": f"{type(e).__name__}: {e}"})
    rec["elapsed_s"] = int(time.time() - t0)
    return rec


def main() -> int:
    E10.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        print(f"[lock] 已有波次锁：{LOCK.read_text(encoding='utf-8').strip()} → 不启动",
              flush=True)
        return 2
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(f"pid={os.getpid()} at={time.strftime('%F %T')} arm=E10WAVE2",
                    encoding="utf-8")
    try:
        return _run_wave()
    finally:
        LOCK.unlink(missing_ok=True)


def _run_wave() -> int:
    gate = load_e9_gate()
    tids = build_manifest()
    cells = [{"tid": t} for t in tids]
    print(f"Arm1 主力波: {len(cells)} 任务 × 3 轮 = {len(cells)*3} 格（冻结清单）",
          flush=True)
    # 断点续跑：已留档 done 的 (baseline_tid, round) 跳过，不重跑不重复计费
    results: list[dict] = []
    if OUT.exists():
        try:
            results = json.loads(OUT.read_text(encoding="utf-8"))
            print(f"[resume] 载入已留档 {len(results)} 格", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[resume] 留档读取失败（备份后重起）：{e}", flush=True)
            return 3
    done_keys = {(r.get("baseline_tid"), r.get("round")) for r in results
                 if r.get("status") == "done"}
    # C1：error/timeout 等非终态格默认跳过重试（不盲目重试防重复计费——留档缺失≠
    # 未跑，人工对账后从 JSON 删行才可重跑）
    settled_keys = {(r.get("baseline_tid"), r.get("round")) for r in results
                    if r.get("status") in CONDITION_LOST_SKIP}
    conditions: dict[int, dict | None] = {}   # C2：基线条件快照（每任务恢复一次）
    done_n = len(done_keys)
    first_cell_ok = False   # C1：首格停波——首格非终态即停（先核验再放量）
    t0, stop_reason = time.time(), None
    for i, cell in enumerate(cells):
        if stop_reason:
            break
        if cell["tid"] not in conditions:
            conditions[cell["tid"]] = load_baseline_condition(cell["tid"])
            if conditions[cell["tid"]] is None:
                print(f"[C2] slot={i} base={cell['tid']} 条件不可恢复（E9 留档无此格）"
                      f"→ condition_lost 不跑", flush=True)
        for round_no in (1, 2, 3):
            if (cell["tid"], round_no) in settled_keys:
                continue
            # 高峰门（用户硬规则）：窗口关闭即停波；00:00–00:30 空隙等开窗
            if not gate.in_offpeak_window():
                import time as _t
                m = _t.localtime().tm_hour * 60 + _t.localtime().tm_min
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
            # 口径闸（2026-11-17 实锤：昨夜 7 格跑成 flash，与 E9-A1 chat 基线
            # 混模型=KB 效应不可分离；Arm 1 对照=E9-A1 chat 零成本，复跑必须同
            # 模型 chat）——每格前兜底复位档位，防其他会话中途切档污染口径。
            gate.restore_profile_default()
            rec = run_cell(cell["tid"], i * 3 + round_no, round_no,
                           condition=conditions[cell["tid"]])
            rec["task_slot"] = i
            results.append(rec)
            if rec.get("status") == "done":
                done_n += 1
                first_cell_ok = True
            # 每格即落盘（断点可查）
            OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"[cell] slot={i} r{round_no} base={cell['tid']} "
                  f"status={rec.get('status')} spent={rec.get('tokens_spent')} "
                  f"elapsed={int(time.time() - t0)}s", flush=True)
            # C1 首格停波：首个实跑格非终态（error/timeout/driver_error）即停波——
            # 先跑 1 格核验再放量（批3 白烧 205k 教训升级）；condition_lost 是 C2
            # 预期动作不算失败，不停波。判断在落盘之后（留档不丢）
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
        if not stop_reason and (i + 1) % 10 == 0:
            el = int((time.time() - t0) / 60)
            print(f"[任务 {i+1}/{len(cells)}] done {done_n}/{len(results)} "
                  f"({el} 分钟)", flush=True)
    if stop_reason is None:
        stop_reason = "plan_exhausted"
    print(f"\n收官: done {done_n}/{len(results)}，stop={stop_reason}，"
          f"本波耗时 {int((time.time()-t0)/60)} 分钟", flush=True)
    print(f"落盘: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
