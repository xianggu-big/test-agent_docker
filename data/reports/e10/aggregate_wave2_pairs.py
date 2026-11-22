"""Wave2 v2 配对差分聚合 v2（2026-11-22 任务4：三表+三轮三层判定+配对键修正）。

设计依据：
- **主口径=K 纵向自配对**：同基线（baseline_tid）×3 轮，KB 逐轮加厚——每条基线
  自己当自己的对照（同需求同源码跨轮差分）。
- **横向 K vs D（任务4 修正配对键）**：配对键=（lang, corpus, requirement）。
  > **修订留痕（2026-11-22 Phase 1.3）**：原设计键=同 lang+corpus+**baseline_tid**
  > +requirement——数据实锤该键恒配 0 对（K r3 有 38 个 baseline、D 臂全部 1 个
  > baseline、交集=0：D 臂基线与 K 不重叠是采样设计使然，不是数据缺失）。
  > matched-pair 的本质=同 requirement 不同处理，baseline_tid 仅作溯源标注
  > 不进键。修订依据=155 格实测（k3∩D 按 requirement 键=3 对）。
- **三轮可比性三层判定**（记忆挂账兑现）：①定性层=detected/pass 序列一致性
  ②定量层=passed/failed/tokens 的 mean±std ③趋势层=Δpass(r3-r1) 方向。
- **三表**：A=K r3 vs D 配对差分（符号检验，n<30 如实标注检验力）；
  B=KB 收敛曲线（Wilson 95% CI）；C=注入成本代价（K-D token 配对差）。
- 幂等：输入只读（results+cells 全量重算），输出覆盖写——重跑零成本零 diff。

用法：python aggregate_wave2_pairs.py → wave2_pairs.json + wave2_pairs.md
（run_wave2.py 收尾自动调用本脚本——常态化接线见 run_wave2 main 尾部。）
"""
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

E10 = Path(__file__).resolve().parent
RESULTS = E10 / "wave2_results_v2.json"
MANIFEST = E10 / "wave2_cells_v2.json"


def detected_of(rec: dict) -> str:
    """与驱动器同源的检出定性（唯一实现，别处引用本函数不复制）。"""
    sr = rec.get("stop_reason") or ""
    if rec.get("verdict") == "stop" and ("疑似产品缺陷" in sr or "证人" in sr):
        return "detected"
    if rec.get("verdict") == "pass":
        return "pass"
    if rec.get("verdict") == "stop":
        return "honest_exit"
    return rec.get("verdict") or rec.get("status") or "error"


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI（小 n 比 Wald 稳）。"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def sign_test(diffs: list[float]) -> tuple[float, int]:
    """符号检验（双侧，n<30 时检验力低——调用方如实标注）。返回 (p, n_nonzero)。"""
    nz = [d for d in diffs if d != 0]
    n = len(nz)
    if n == 0:
        return (1.0, 0)
    k = sum(1 for d in nz if d > 0)
    # 双侧精确二项检验 p=0.5
    tail = sum(math.comb(n, i) for i in range(min(k, n - k) + 1)) / (2 ** n)
    return (min(1.0, 2 * tail), n)


def ms(vals: list) -> str:
    """mean±std 文本（n=1 时只报值；None 当 0——格未执行到的字段）。"""
    v = [x or 0 for x in vals]
    if not v:
        return "-"
    if len(v) == 1:
        return f"{v[0]:g}"
    return f"{statistics.mean(v):.1f}±{statistics.stdev(v):.1f}"


def main() -> int:
    recs = json.loads(RESULTS.read_text(encoding="utf-8"))
    specs = {s["key"]: s for s in json.loads(MANIFEST.read_text(encoding="utf-8"))}

    def sp_of(r: dict) -> dict:
        return specs.get(r.get("key"), {})

    # —— K 纵向分组（同 baseline_tid×3 轮——自配对键不变） ——
    groups = defaultdict(lambda: {"kb": {}, "dist": []})
    for r in recs:
        sp = sp_of(r)
        lang = r.get("lang") or sp.get("lang")
        ctype = r.get("ctype") or sp.get("ctype")
        base = r.get("baseline_tid") or sp.get("baseline_tid")
        rnd = r.get("round") or sp.get("round")
        anchor = r.get("anchor") or sp.get("anchor")
        if ctype == "kb" and base and base != "None":
            groups[(lang, str(base), anchor)]["kb"][int(rnd or 0)] = r
        elif ctype == "dist":
            groups[(lang, str(base or "dist"), anchor)]["dist"].append(r)

    # —— 主口径：K 纵向三轮 + 三层判定 ——
    kb_rows = []
    trend = defaultdict(lambda: {"detected": 0, "n": 0, "passed": [], "tokens": []})
    qual_consistent = 0
    for (lang, base, anchor), g in sorted(
            groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), str(kv[0][2]))):
        rounds = g["kb"]
        if not rounds:
            continue
        det_seq = []
        row = {"lang": lang, "baseline": base, "anchor": anchor, "rounds": {}}
        for rnd in (1, 2, 3):
            r = rounds.get(rnd)
            if r is None:
                row["rounds"][rnd] = "missing"
                det_seq.append(None)
                continue
            det = detected_of(r)
            det_seq.append(det)
            trend[rnd]["n"] += 1
            trend[rnd]["detected"] += (det == "detected")
            trend[rnd]["passed"].append(r.get("passed") or 0)
            trend[rnd]["tokens"].append(r.get("tokens_spent") or 0)
            row["rounds"][rnd] = {"det": det, "passed": r.get("passed"),
                                  "failed": r.get("failed"),
                                  "tokens": r.get("tokens_spent")}
        row["trajectory"] = " → ".join(d or "?" for d in det_seq)
        # 三层判定（任务4 核心新增）
        present = [d for d in det_seq if d is not None]
        row["layer1_qual"] = ("一致(" + present[0] + ")" if present and len(set(present)) == 1
                              else f"分歧({len(set(present))}种)")
        if len(set(present)) == 1 and present:
            qual_consistent += 1
        pv = [row["rounds"][i]["passed"] for i in (1, 2, 3)
              if row["rounds"].get(i) != "missing"]
        fv = [row["rounds"][i]["failed"] for i in (1, 2, 3)
              if row["rounds"].get(i) != "missing"]
        tv = [row["rounds"][i]["tokens"] for i in (1, 2, 3)
              if row["rounds"].get(i) != "missing"]
        row["layer2_quant"] = {"passed": ms(pv), "failed": ms(fv), "tokens": ms(tv)}
        if rounds.get(1) and rounds.get(3):
            dp = (rounds[3].get("passed") or 0) - (rounds[1].get("passed") or 0)
            df = (rounds[3].get("failed") or 0) - (rounds[1].get("failed") or 0)
            row["delta_pass_r3_r1"] = dp
            row["delta_fail_r3_r1"] = df
            row["layer3_trend"] = ("上升" if dp > 0 else "下降" if dp < 0 else "持平")
        kb_rows.append(row)

    # —— 横向 K r3 vs D（任务4 修正配对键=lang+corpus+requirement） ——
    def pair_id(r: dict) -> tuple:
        sp = sp_of(r)
        return (r.get("lang") or sp.get("lang"),
                r.get("corpus") or sp.get("corpus"),
                (sp.get("requirement") or r.get("requirement") or "").strip())

    k_all = [r for r in recs if (r.get("ctype") or sp_of(r).get("ctype")) == "kb"]
    d_all = [r for r in recs if (r.get("ctype") or sp_of(r).get("ctype")) == "dist"]
    # K 侧取 r3（KB 最厚；缺 r3 回退该基线最大轮）
    k_by_pid = {}
    for r in k_all:
        sp = sp_of(r)
        base = r.get("baseline_tid") or sp.get("baseline_tid")
        rnd = int(r.get("round") or sp.get("round") or 0)
        pid = pair_id(r)
        cur = k_by_pid.get(pid)
        if cur is None or rnd > int(cur.get("round") or 0):
            k_by_pid[pid] = r
    d_by_pid = defaultdict(list)
    for r in d_all:
        d_by_pid[pair_id(r)].append(r)

    pairs = []
    unpaired_k = []
    for pid, k in sorted(k_by_pid.items(), key=lambda kv: str(kv[0])):
        ds = d_by_pid.get(pid)
        if not ds:
            unpaired_k.append(pid)
            continue
        # 一 K 多 D：D 侧先聚合（均值）再成一对——保符号检验的配对独立性
        # （首版一 K 配多 D：js-calc 一格 K 配 11 个 D=31 对里 K 侧重复出现，n 失真）
        d_n = len(ds)
        d_det = "detected" if sum(detected_of(d) == "detected" for d in ds) > d_n / 2 else "not_detected"
        d_pass = statistics.mean([d.get("passed") or 0 for d in ds])
        d_tok = statistics.mean([d.get("tokens_spent") or 0 for d in ds])
        pairs.append({
            "lang": pid[0], "corpus": pid[1],
            "requirement": (pid[2] or "")[:60],
            "k_round": k.get("round"), "k_base": k.get("baseline_tid"),
            "d_n": d_n, "d_bases": [d.get("baseline_tid") for d in ds],
            "k_det": detected_of(k), "d_det": d_det, "d_det_rate":
                f"{sum(detected_of(d) == 'detected' for d in ds)}/{d_n}",
            "k_passed": k.get("passed"), "d_passed": round(d_pass, 1),
            "k_tokens": k.get("tokens_spent"), "d_tokens": round(d_tok, 0),
        })
    unpaired_d = [pid for pid in d_by_pid if pid not in k_by_pid]

    # 表 A 统计：detected 差（K detected 且 D 非 detected=+1…离散口径）与 passed/token 差
    det_diff = [1 if (p["k_det"] == "detected") - (p["d_det"] == "detected") > 0
                else -1 if (p["k_det"] == "detected") - (p["d_det"] == "detected") < 0 else 0
                for p in pairs]
    pass_diff = [(p["k_passed"] or 0) - (p["d_passed"] or 0) for p in pairs]
    tok_diff = [(p["k_tokens"] or 0) - (p["d_tokens"] or 0) for p in pairs]
    pA_det, nA_det = sign_test(det_diff)
    pA_pass, nA_pass = sign_test(pass_diff)
    pC_tok, nC_tok = sign_test(tok_diff)

    # —— 三表输出 ——
    lines = ["# Wave2 v2 配对差分（任务4 v2：三表+三层判定+配对键修正）", ""]
    lines.append(f"格数 {len(recs)}（K {len(k_all)} / D {len(d_all)}）；"
                 f"K 纵向配对组 {len(kb_rows)}；横向 K3-vs-D 配对 {len(pairs)} 对"
                 f"（unpaired：K 侧 {len(unpaired_k)} / D 侧 {len(unpaired_d)}——D 臂 146 格续跑后补齐）")
    lines.append("")
    lines.append("> 配对键修订留痕：原设计含 baseline_tid——数据实锤恒配 0 对"
                 "（K r3 有 38 基线、D 臂共享 1 基线、交集 0=采样设计使然），"
                 "修正为 lang+corpus+requirement（matched-pair 本质=同需求不同处理）。")
    lines.append("")

    lines.append("## 表 A · K vs D 配对差分（matched-pair，K 取最厚 KB 轮）")
    lines.append("")
    lines.append("| 指标 | K 均值 | D 均值 | 配对差均值 | 符号检验 p | 有效 n |")
    lines.append("|---|---|---|---|---|---|")
    if pairs:
        lines.append(
            f"| detected 率 | {sum(p['k_det']=='detected' for p in pairs)/len(pairs):.2f} | "
            f"{sum(p['d_det']=='detected' for p in pairs)/len(pairs):.2f} | "
            f"{statistics.mean(det_diff):+.2f} | {pA_det:.3f} | {nA_det}/{len(pairs)} |")
        lines.append(
            f"| passed | {statistics.mean([p['k_passed'] or 0 for p in pairs]):.1f} | "
            f"{statistics.mean([p['d_passed'] or 0 for p in pairs]):.1f} | "
            f"{statistics.mean(pass_diff):+.1f} | {pA_pass:.3f} | {nA_pass}/{len(pairs)} |")
        lines.append(
            f"| tokens | {statistics.mean([p['k_tokens'] or 0 for p in pairs]):.0f} | "
            f"{statistics.mean([p['d_tokens'] or 0 for p in pairs]):.0f} | "
            f"{statistics.mean(tok_diff):+.0f} | {pC_tok:.3f} | {nC_tok}/{len(pairs)} |")
    else:
        lines.append("|（无配对——D 臂未覆盖 K 的 requirement，续跑后出表）| | | | | |")
    lines.append("")
    if pairs and len(pairs) < 30:
        lines.append(f"**检验力诚实标注**：当前配对 n={len(pairs)}<30，符号检验最小可达"
                     f" p={0.5 ** min(nA_det, len(pairs)) if pairs else 1:.3f}"
                     "——读数作方向性参考，终表以 146 格补齐后为准。")
        lines.append("")
    lines.append("配对明细：")
    lines.append("")
    lines.append("| lang | corpus | requirement | K det/passed | D det/passed | Δpass |")
    lines.append("|---|---|---|---|---|---|")
    for p in pairs:
        lines.append(f"| {p['lang']} | {p['corpus']} | {p['requirement']} | "
                     f"{p['k_det']}/{p['k_passed']} | {p['d_det']}/{p['d_passed']} | "
                     f"{(p['k_passed'] or 0) - (p['d_passed'] or 0):+.1f} |")
    lines.append("")

    lines.append("## 表 B · KB 收敛曲线（K 纵向，Wilson 95% CI）")
    lines.append("")
    lines.append("| 轮 | n | detected 率 [CI] | passed 均值±σ | tokens 均值±σ |")
    lines.append("|---|---|---|---|---|")
    for rnd in (1, 2, 3):
        t = trend.get(rnd, {"n": 0, "detected": 0, "passed": [], "tokens": []})
        n = t["n"]
        lo, hi = wilson_ci(t["detected"], n)
        pm = ms(t["passed"]) if len(t["passed"]) > 1 else (f"{t['passed'][0]:g}" if t["passed"] else "-")
        tm = ms(t["tokens"]) if len(t["tokens"]) > 1 else (f"{t['tokens'][0]:g}" if t["tokens"] else "-")
        rate = f"{t['detected']/n:.2f} [{lo:.2f}, {hi:.2f}]" if n else "-"
        lines.append(f"| r{rnd} | {n} | {rate} | {pm} | {tm} |")
    lines.append("")

    lines.append("## 表 C · 注入成本代价（K tokens − D tokens，配对差）")
    lines.append("")
    if pairs:
        lines.append(f"配对差均值 {statistics.mean(tok_diff):+.0f} tokens"
                     f"（中位 {statistics.median(tok_diff):+.0f}）；"
                     f"K 侧独有成本=KB 注入的 token 开销实证。")
    else:
        lines.append("（无配对——同表 A）")
    lines.append("")

    lines.append("## K 纵向三层判定（每基线）")
    lines.append("")
    lines.append(f"定性层全一致组：{qual_consistent}/{len(kb_rows)}")
    lines.append("")
    lines.append("| 基线 | 轨迹（定性层） | 定量层 passed | 趋势层 Δpass |")
    lines.append("|---|---|---|---|")
    for row in kb_rows:
        lines.append(f"| {row['lang']}/base{row['baseline']} [{row['anchor']}] | "
                     f"{row['trajectory']}（{row['layer1_qual']}） | "
                     f"{row['layer2_quant']['passed']} | "
                     f"{row.get('layer3_trend', '-')}"
                     f"（{row.get('delta_pass_r3_r1', '-')}） |")
    lines.append("")
    if unpaired_k:
        lines.append("## unpaired（K 侧无 D 对——如实列出）")
        lines.append("")
        for pid in unpaired_k[:20]:
            lines.append(f"- {pid[0]}/{pid[1]}: {(pid[2] or '')[:70]}")
        if len(unpaired_k) > 20:
            lines.append(f"- …共 {len(unpaired_k)} 条")
        lines.append("")

    out_json = E10 / "wave2_pairs.json"
    out_json.write_text(json.dumps(
        {"kb_rows": kb_rows, "trend": {str(k): v for k, v in trend.items()},
         "pairs": pairs, "unpaired_k": [list(p) for p in unpaired_k],
         "unpaired_d": [list(p) for p in unpaired_d],
         "pairing_key": "lang+corpus+requirement",
         "sign_tests": {"det": [pA_det, nA_det], "pass": [pA_pass, nA_pass],
                        "tokens": [pC_tok, nC_tok]}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    out_md = E10 / "wave2_pairs.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:40]))
    print(f"\n落盘: {out_json} / {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
