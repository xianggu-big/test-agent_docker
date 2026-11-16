# -*- coding: utf-8 -*-
"""摘要更新：补多语言六语言验收句（中英双语，一次性脚本）。"""
from pathlib import Path

P = Path(r"C:\Users\xianggu\Desktop\毕设\论文\摘要.md")
s = P.read_text(encoding="utf-8")

# 中文摘要：在"本方法为可信……"前插入多语言句
old_zh = "质量组件不损伤检出率并出现正向信号。本方法为可信、可审计、可复现的大模型自动化测试提供了一个完整参考实现。"
new_zh = ("质量组件不损伤检出率并出现正向信号。执行链路已通过六语言（Python/JavaScript/"
          "TypeScript/Java/Go/PHP）18 格全链路验收扩展至多语言，语言生态相关缺陷经"
          "\"失败即诊断修复重跑\"闭环全部治理，链路故障零残留。本方法为可信、可审计、"
          "可复现的大模型自动化测试提供了一个完整参考实现。")
assert old_zh in s, "zh anchor not found"
s = s.replace(old_zh, new_zh, 1)

# 英文摘要：对应句
old_en = ("quality components do not harm detection and show positive signals. "
          "The approach provides a complete reference implementation for trustworthy, "
          "auditable, and reproducible LLM-based automated testing.")
new_en = ("quality components do not harm detection and show positive signals. The "
          "execution pipeline has been validated across six languages (Python, "
          "JavaScript, TypeScript, Java, Go, and PHP) via an 18-cell full-pipeline "
          "acceptance run with zero residual environment faults, in which five "
          "language-ecosystem-related defects were diagnosed and fixed through a "
          "failure-driven repair loop. The approach provides a complete reference "
          "implementation for trustworthy, auditable, and reproducible LLM-based "
          "automated testing.")
assert old_en in s, "en anchor not found"
s = s.replace(old_en, new_en, 1)

P.write_text(s, encoding="utf-8")
print("摘要更新完成（中英双语多语言句）")
