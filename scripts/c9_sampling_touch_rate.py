# -*- coding: utf-8 -*-
"""c9_sampling_touch_rate.py——"需求无关的采样触达率"统计（总包 C9，2026-11-27）。

口径（用户授权执行者定，2026-11-26）：go-api-book 的 DELETE 检出不写成"需求驱动的
缺陷检出能力"（需求 #1159 全文 39 字不含缺陷线索），写成"需求无关的采样触达率"——
v1 10/10 与 v3 12/15 的差用 Fisher 精确检验，合并比例用 Wilson 95% CI，与 bench3
的需求显式化梯度（94.4%→86.7%→0%）并列。

数字以本脚本输出为准（纪律：不凭记忆转述）。
用法：python -I scripts/c9_sampling_touch_rate.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float:
    """Fisher 精确检验（双侧，超几何）；四数=[臂1检出, 臂1未检, 臂2检出, 臂2未检]。"""
    from math import comb
    n = a + b + c + d
    k = a + c                      # 检出总数
    r1 = a + b                     # 臂1 行和
    lo, hi = max(0, k - (n - r1)), min(r1, k)
    if lo > hi:
        return 1.0
    p_obs = comb(r1, a) * comb(n - r1, k - a)
    weights = [comb(r1, x) * comb(n - r1, k - x) for x in range(lo, hi + 1)]
    total = sum(weights)
    p = sum(w for w in weights if w <= p_obs * (1 + 1e-9)) / total
    return min(1.0, p)


def wilson_ci(k: int, n: int, z: float = 1.959963985) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def main() -> int:
    v1_k, v1_n = 10, 10          # v1 测量批 go DELETE（泛需求 #1159）
    v3_k, v3_n = 12, 15          # v3 规模批 go DELETE（语义口径）
    p = fisher_exact_2x2(v1_k, v1_n - v1_k, v3_k, v3_n - v3_k)
    mk, mn = v1_k + v3_k, v1_n + v3_n
    lo, hi = wilson_ci(mk, mn)
    lo1, hi1 = wilson_ci(v1_k, v1_n)
    lo3, hi3 = wilson_ci(v3_k, v3_n)
    print("== C9 采样触达率统计（脚本输出原文）==")
    print(f"v1: {v1_k}/{v1_n}  Wilson95=[{lo1:.3f}, {hi1:.3f}]")
    print(f"v3: {v3_k}/{v3_n}  Wilson95=[{lo3:.3f}, {hi3:.3f}]")
    print(f"Fisher 精确（双侧）v1 vs v3: p={p:.4f}")
    print(f"合并: {mk}/{mn}={mk / mn:.3f}  Wilson95=[{lo:.3f}, {hi:.3f}]")
    print("结论口径：无统计学差异 → 写'需求无关的采样触达率'，")
    print("不得写'需求驱动的缺陷检出能力'；与 bench3 梯度（94.4%→86.7%→0%）并列。")
    out = ROOT / "data" / "reports" / "agentloop_scale" / "c9_stats.json"
    out.write_text("""{"v1": {"k": %d, "n": %d, "wilson95": [%.4f, %.4f]},
 "v3": {"k": %d, "n": %d, "wilson95": [%.4f, %.4f]},
 "fisher_p": %.4f, "merged": {"k": %d, "n": %d, "p": %.4f, "wilson95": [%.4f, %.4f]}}
""" % (v1_k, v1_n, lo1, hi1, v3_k, v3_n, lo3, hi3, p, mk, mn, mk / mn, lo, hi),
        encoding="utf-8")
    print(f"[written] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
