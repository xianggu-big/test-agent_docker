# 验收需求（ts-calc，TypeScript 执行链路·白包阶段1）

被测模块 src/calc.ts，三组行为：

1. add(a, b)：返回两数之和（等价类：正数/负数/零组合）
2. clamp(v, lo, hi)：把 v 钳制到闭区间 [lo, hi]——v < lo 返回 lo；v > hi 返回 hi；
   区间内（含 v==lo 与 v==hi 两端点）原样返回 v（边界值分析重点）
3. isValidCode(code)：2~8 位字母数字串返回 true，否则 false
