"""三方案优化效果测量（离线确定性：不触网、不依赖 Docker、LLM 全走 fake 注入）。

用法：python scripts/optimization_check.py
四个测量（每项给出"优化前 → 优化后"的量化数字，并对可断言的契约做硬断言）：
  OPT-A 断言质量闸：合成 8 用例套件（3 全弱/4 强/1 混合）→ 弱断率 before/after，
       硬断言=闸后弱断率恰为 0 且只剔全弱、混合用例保留；
  OPT-B PBT 第七法：宿主真机 A/B——同一被测模块，示例套件 E vs 示例+属性套件 P，
       跑变异仪器（scripts/mutation_kill 同款机制）量 kill_rate 增量 Δ；
       硬断言=两套件基线全绿 + 变异体数>0（Δ 本身是测量值，不预设方向）；
  OPT-C 条件缺口回灌：3 条件 1 覆盖 → 定向补 1 轮 → RTM 条件覆盖 1/3→3/3，
       硬断言=LLM 恰调用 1 次（预算纪律）+ 缺口清零 + 无缺口时 0 调用；
  仪器复算：data/corpus/*/mutation_task*.json 明细逐份重算 kill_rate 与落盘值比对
       （自洽性），并复现 corpus-001 vs corpus-002 套件有效性差异口径。

诚实边界：OPT-B 的属性用例是与朴素参照实现的等价性质（oracle 与实现同构，
测的是"杀变异体的能力"即方案B独有指标，不是 oracle 独立性——后者是 V7 语料 v2 课题）；
变异算子集与正式仪器同款（cmp/bool/binop 翻转），数字只做方案内前后对比。
临时目录固定 data/tmp_mut（D 盘）——C 盘 Temp junction 坑，勿改。
报告落 data/reports/optimization_check_<ts>.json（运行产物，不入库）。
"""
import ast
import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.gen_tests import (apply_assertion_gate,  # noqa: E402
                                         refill_condition_gaps)

VIOLATIONS: list[str] = []


def _load_mk():
    p = ROOT / "scripts" / "mutation_kill.py"
    spec = importlib.util.spec_from_file_location("mutation_kill", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _require(name: str, cond: bool, detail: str):
    print(f"    {'✓' if cond else '✗'} {name}: {detail}")
    if not cond:
        VIOLATIONS.append(f"{name}: {detail}")


# ---------------------------------------------------------------- OPT-A
def opt_a() -> dict:
    print("\n" + "=" * 72)
    print("OPT-A 断言质量闸：弱断率 before/after（合成套件 8 用例）")
    suite = [
        ("t_eq_等价类", "def t_eq():\n    assert add(1, 2) == 3\n"),
        ("t_raises_异常", "def t_r():\n    import pytest\n"
                         "    with pytest.raises(ValueError):\n        parse(-1)\n"),
        ("t_in_成员", "def t_in():\n    assert pick(2) in [1, 2]\n"),
        ("t_chain_边界", "def t_ch():\n    assert 0 < clamp(15) <= 10\n"),
        ("t_mixed_混合", "def t_mx():\n    r = add(1, 2)\n    assert r\n    assert r == 3\n"),
        ("t_bare_裸真值", "def t_b():\n    r = add(1, 2)\n    assert r\n"),
        ("t_none_非空", "def t_n():\n    assert add(1, 2) is not None\n"),
        ("t_noassert_无断言", "def t_na():\n    add(1, 2)\n"),
    ]
    cases = [{"name": n, "code": c} for n, c in suite]
    kept_off, _, stats_off = apply_assertion_gate(cases, gate=False)
    rate_before = round(stats_off["weak_cases"] / stats_off["cases"], 3)
    kept, rejected, stats_on = apply_assertion_gate(cases, gate=True)
    # 闸后弱断率必须对"保留批"重算（stats_on.weak_cases 是输入批口径，不能直接除保留数）
    _, _, stats_kept = apply_assertion_gate(kept, gate=False)
    rate_after = round(stats_kept["weak_cases"] / stats_kept["cases"], 3) if kept else None
    print(f"    弱断率（闸关）= {stats_off['weak_cases']}/{stats_off['cases']}"
          f" = {rate_before}；断言总数 {stats_off['assert_total']}"
          f"（弱 {stats_off['assert_weak']}）")
    print(f"    弱断率（闸开，保留批重算）= {stats_kept['weak_cases']}/{stats_kept['cases']}"
          f" = {rate_after}；剔除 {stats_on['gate_rejected']} 条：{rejected}")
    _require("A·闸后弱断率=0", rate_after == 0.0, f"rate_after={rate_after}")
    _require("A·恰剔 3 条全弱（混合用例保留）",
             stats_on["gate_rejected"] == 3
             and [c["name"] for c in kept] == [n for n, _ in suite[:5]],
             f"保留={[c['name'] for c in kept]}")
    _require("A·闸关时只算不剔（度量恒算语义）",
             len(kept_off) == 8 and "gate_rejected" not in stats_off, "恒算口径成立")
    return {"weak_rate_before": rate_before, "weak_rate_after": rate_after,
            "rejected": stats_on["gate_rejected"]}


# ---------------------------------------------------------------- OPT-B
PRICING = '''def final_price(unit_cents, vip, quantity):
    total = unit_cents * quantity
    if vip and total >= 10000:
        total = total - total // 20
    if total >= 5000 and quantity > 10:
        total = total - 300
    return total
'''

TEST_E = '''import pytest
from pricing import final_price

def test_basic_等价类():
    assert final_price(100, False, 1) == 100

def test_vip_等价类():
    assert final_price(1000, True, 10) == 9500

def test_bulk_等价类():
    assert final_price(500, False, 11) == 5200

def test_vip_bulk_等价类():
    assert final_price(600, True, 20) == 11100

def test_threshold_边界值分析():
    assert final_price(9999, True, 1) == 9999
'''

PROPERTY = '''

def _ref(unit_cents, vip, quantity):
    t = unit_cents * quantity
    if vip and t >= 10000:
        t = t - t // 20
    if t >= 5000 and quantity > 10:
        t = t - 300
    return t

from hypothesis import given, settings
from hypothesis import strategies as st

@settings(max_examples=20, deadline=None)
@given(st.integers(min_value=1, max_value=2000), st.booleans(),
       st.integers(min_value=1, max_value=60))
def test_equiv_ref_属性测试(unit, vip, qty):
    assert final_price(unit, vip, qty) == _ref(unit, vip, qty)
'''


def _mk_ws(tmp_root: Path, tag: str, test_src: str) -> Path:
    ws = Path(tempfile.mkdtemp(prefix=f"optB_{tag}_", dir=str(tmp_root)))
    (ws / "pricing.py").write_text(PRICING, encoding="utf-8")
    (ws / "test_pricing.py").write_text(test_src, encoding="utf-8")
    return ws


def _kill_rate(mk, ws: Path, cap: int = 12) -> dict:
    mods = mk.source_modules(ws)
    baseline_fail, baseline_ok = mk._suite_run(ws)
    if not baseline_ok:
        return {"error": "基线收集失败"}
    mutants = []
    tmp = Path(tempfile.mkdtemp(prefix="optB_mut_", dir=str(ws.parent)))
    try:
        for mod in mods:
            src = mod.read_text(encoding="utf-8")
            for i in range(cap):
                got = mk._mutate(src, i)
                if got is None:
                    break
                mutated, desc = got
                shutil.copytree(ws, tmp, dirs_exist_ok=True)
                (tmp / mod.relative_to(ws)).write_text(mutated, encoding="utf-8")
                fail, _ = mk._suite_run(tmp)
                mutants.append({"site": f"{mod.name}#{i}", "mutation": desc,
                                "killed": bool(fail - baseline_fail)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    killed = sum(1 for m in mutants if m["killed"])
    return {"baseline_green": not baseline_fail, "mutants": len(mutants),
            "killed": killed,
            "kill_rate": round(killed / len(mutants), 3) if mutants else None,
            "survived_sites": [f"{m['site']}({m['mutation']})" for m in mutants
                               if not m["killed"]],
            "detail": mutants}


def opt_b(mk) -> dict:
    print("\n" + "=" * 72)
    print("OPT-B PBT 第七法：示例套件 E vs 示例+属性套件 P 的变异杀死率（宿主真机 A/B）")
    tmp_root = ROOT / "data" / "tmp_mut"
    tmp_root.mkdir(parents=True, exist_ok=True)
    res = {}
    for tag, test_src in (("E", TEST_E), ("P", TEST_E + PROPERTY)):
        ws = _mk_ws(tmp_root, tag, test_src)
        try:
            r = _kill_rate(mk, ws)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        res[tag] = r
        if "error" in r:
            print(f"    [{tag}] {r['error']}")
            continue
        print(f"    套件{tag}: 基线{'绿' if r['baseline_green'] else '红'} · "
              f"变异体 {r['mutants']} · 杀死 {r['killed']} · "
              f"kill_rate={r['kill_rate']} · 存活位={r['survived_sites']}")
    e, p = res.get("E", {}), res.get("P", {})
    if "kill_rate" in e and "kill_rate" in p:
        delta = round(p["kill_rate"] - e["kill_rate"], 3)
        print(f"    Δkill(P−E) = {delta:+.3f}（属性用例对杀死率的增量，测量值不预设方向）")
        _require("B·两套件基线全绿", e.get("baseline_green") and p.get("baseline_green"),
                 f"E={e.get('baseline_green')} P={p.get('baseline_green')}")
        _require("B·变异体集合非空且同源", e.get("mutants", 0) > 0
                 and e.get("mutants") == p.get("mutants"),
                 f"E={e.get('mutants')} P={p.get('mutants')}（同模块同算子集）")
    return res


# ---------------------------------------------------------------- OPT-C
class _Resp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return _Resp(self.payload)


CONDS = [{"condition": "空密码应判为无效"},
         {"condition": "长度恰为上限的密码应有效"},
         {"condition": "负数金额应抛出异常"}]
CASE_C1 = {"name": "test_空密码应判为无效_边界值分析", "expected": "无效",
           "code": "def test_空密码应判为无效_边界值分析():\n    assert check('') is False\n"}
REFILL_PAYLOAD = json.dumps({"cases": [
    {"name": "test_长度恰为上限的密码应有效_边界值分析",
     "code": "def test_长度恰为上限的密码应有效_边界值分析():\n"
             "    assert check('x' * 10) is True\n"},
    {"name": "test_负数金额应抛出异常_错误推测法",
     "code": "def test_负数金额应抛出异常_错误推测法():\n"
             "    assert check('-1') is False\n"}]}, ensure_ascii=False)


def opt_c() -> dict:
    print("\n" + "=" * 72)
    print("OPT-C 条件缺口回灌：RTM 条件覆盖 1/3 → 3/3（fake LLM 注入，预算纪律）")
    from test_agent.skills.gen_tests import _condition_gaps
    tmp_root = ROOT / "data" / "tmp_mut"
    tmp_root.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="optC_", dir=str(tmp_root)))
    llm = _FakeLLM(REFILL_PAYLOAD)
    new, imports, info = refill_condition_gaps(
        llm, [("m.py", "def check(p):\n    return bool(p)\n")], "密码校验",
        CONDS, [CASE_C1], ws, {CASE_C1["name"]}, True, False)
    shutil.rmtree(ws, ignore_errors=True)
    cov_before = f"{len(CONDS) - info['uncovered_before']}/{len(CONDS)}"
    cov_after = f"{len(CONDS) - info['uncovered_after']}/{len(CONDS)}"
    print(f"    覆盖 {cov_before} → {cov_after}；补生成 {info['refill_generated']} 条；"
          f"LLM 调用 {llm.calls} 次；info={info}")
    _require("C·缺口清零（1/3→3/3）",
             info["uncovered_before"] == 2 and info["uncovered_after"] == 0,
             f"before={info['uncovered_before']} after={info['uncovered_after']}")
    _require("C·LLM 恰调用 1 次（补生成恰 1 轮的预算纪律）", llm.calls == 1,
             f"calls={llm.calls}")

    full = [CASE_C1] + new
    llm2 = _FakeLLM("{}")
    refill_condition_gaps(
        llm2, [("m.py", "x = 1")], "", CONDS, full,
        ROOT / "data" / "tmp_mut", {c["name"] for c in full}, True, False)
    _require("C·无缺口时 0 调用（不做无用功）", llm2.calls == 0, f"calls={llm2.calls}")
    gaps_left = _condition_gaps(CONDS, full)
    _require("C·补生成用例经 2-gram 真实对准（非仅 LLM 自称）", gaps_left == [],
             f"剩余缺口={[g['condition'] for g in gaps_left]}")
    return {"coverage_before": cov_before, "coverage_after": cov_after,
            "refill_generated": info["refill_generated"], "llm_calls": llm.calls}


# ------------------------------------------------------- 仪器明细自洽复算
def instrument_recheck() -> dict:
    print("\n" + "=" * 72)
    print("仪器复算：corpus 变异明细逐份重算 kill_rate，与落盘值比对")
    files = sorted((ROOT / "data" / "corpus").glob("*/mutation_task*.json"))
    if not files:
        print("    （无明细文件，跳过——诚实标注不硬造）")
        return {"files": 0}
    per_corpus: dict[str, list[float]] = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        detail_killed = sum(1 for m in d.get("detail", []) if m.get("killed"))
        total = d.get("mutants_total")
        if not total:
            continue
        recomputed = round(detail_killed / total, 3)
        stored = d.get("kill_rate")
        _require(f"自洽 {f.parent.name}/{f.stem}",
                 stored is not None and abs(recomputed - stored) <= 0.002,
                 f"重算={recomputed} 落盘={stored}（{detail_killed}/{total}）")
        per_corpus.setdefault(f.parent.name, []).append(stored)
    for corpus, rates in sorted(per_corpus.items()):
        mean = sum(rates) / len(rates)
        print(f"    {corpus}: n={len(rates)} 份，kill_rate 均值 {mean:.3f}")
    if len(per_corpus) >= 2:
        print("    （方案B 独有指标横比：corpus-002 套件有效性差异口径的复现出口）")
    return {"files": len(files),
            "corpus_mean": {k: round(sum(v) / len(v), 3)
                            for k, v in per_corpus.items()}}


def main() -> int:
    ts = time.strftime("%Y%m%d-%H%M%S")
    mk = _load_mk()
    report: dict = {"ts": ts}
    report["OPT_A_gate"] = opt_a()
    report["OPT_B_pbt_ab"] = opt_b(mk)
    report["OPT_C_refill"] = opt_c()
    report["instrument_recheck"] = instrument_recheck()

    print("\n" + "=" * 72)
    if VIOLATIONS:
        print(f"优化效果测量完成：{len(VIOLATIONS)} 项硬断言违约 ↓")
        for v in VIOLATIONS:
            print(f"  ✗ {v}")
    else:
        print("优化效果测量完成：全部硬断言通过（测量值如实输出，不预设方向）")
    rep = ROOT / "data" / "reports" / f"optimization_check_{ts}.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已落 {rep.relative_to(ROOT)}")
    return 1 if VIOLATIONS else 0


if __name__ == "__main__":
    sys.exit(main())
