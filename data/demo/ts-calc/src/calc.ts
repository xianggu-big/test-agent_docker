/** TS 执行链路验收 demo（白包阶段1）：与 js-calc 同款被测形态，加了类型标注。
 *
 * 埋缺陷（与 js-calc 闭区间缺陷同款语义）：clamp 只钳了下界，上界漏钳——
 * 等价类用例（区间内）全过、边界值用例（v==hi / v>hi）失败 = 边界值分析的证人。
 */

export function add(a: number, b: number): number {
  return a + b;
}

export function clamp(v: number, lo: number, hi: number): number {
  if (v < lo) {
    return lo;
  }
  return v; // 缺陷：上界未钳制——clamp(11, 0, 10) 应返回 10，实际返回 11
}

export function isValidCode(code: string): boolean {
  return /^[A-Za-z0-9]{2,8}$/.test(code);
}
