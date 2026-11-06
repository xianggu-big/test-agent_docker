# 验收需求（java-shop，Java 执行链路·白包阶段2）

被测类 com.demo.OrderService，三组行为：

1. applyDiscount(amount, percent)：按百分比打折；percent 越界（<0 或 >100）抛
   IllegalArgumentException（等价类：0/50/100 有效，-1/101 无效）
2. getTier(score)：score >= 90 返回 "A"；score >= 60 返回 "B"；否则 "C"
   （边界值分析重点——以下边界点每点都必须有用例：90 与 89（A 档下界两侧）、
   60 与 59（B 档下界两侧）；例如 score=90 时返回 "A"）
3. isValidCode(code)：2~8 位字母数字串返回 true，否则 false（null 与空串为 false）
