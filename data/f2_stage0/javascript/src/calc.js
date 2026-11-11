// 简易计算器（JS 执行链路 Tier2 演示项目）
function add(a, b) {
  if (typeof a !== "number" || typeof b !== "number") throw new TypeError("参数必须是数字");
  return a + b;
}
function div(a, b) {
  if (b === 0) throw new RangeError("除数不能为零");
  return a / b;
}
function clamp(x, lo, hi) {
  // 故意埋的缺陷：上界用了 >= 导致 x==hi 时返回 hi-1（需求：闭区间）
  if (x < lo) return lo;
  if (x > hi) return hi - 1;
  return x;
}
module.exports = { add, div, clamp };
