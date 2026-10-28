"""按学院《2027 届毕业设计工作计划表》校正开题 docx 的工作进度表与指导时间。

官方硬节点（智能学院-2027届毕业论文（设计）工作计划表.xls）：
- 开题：2026-11-06（统一填写日期）
- 初稿+中期检查：第二学期第 1 周（约 2027-02-25 起），要求系统可运行且提交论文初稿，线下当面检查
- 二稿：第二学期第 2—5 周
- 定稿：第二学期第 7 周前（截止 2027-04-09），第 8 周答辩前论文审查
- 初次答辩：第二学期第 9 周（2027-04-24）
- 二次答辩：第二学期第 11 周（2027-05-12）
"""
from docx import Document

PATH = "docs/开题报告-课题论证-草稿.docx"
d = Document(PATH)


def set_cell_text(cell, text):
    p = cell.paragraphs[0]
    runs = p.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
        # 清掉单元格内多余段落（保留第一个）
        for extra in cell.paragraphs[1:]:
            extra._p.getparent().remove(extra._p)
    else:
        p.add_run(text)


# ---- 1) 第一阶段指导时间：2025 → 2026 ----
fixed = False
for row in d.tables[0].rows:
    for cell in row.cells:
        for p in cell.paragraphs:
            for r in p.runs:
                if "2025" in r.text:
                    r.text = r.text.replace("2025", "2026")
                    fixed = True
print("指导时间 2025→2026 已改:", fixed)

# ---- 2) 工作进度表（T2 R2-R6）----
PLAN = [
    ("2026.10—2026.11.06",
     "选题调研与文献阅读（LLM 测试生成、容器化测试、软件测试理论）；完成开题报告并参加开题答辩"
     "（开题日期按学院要求统一填写 2026 年 11 月 6 日）"),
    ("2026.11—2027.01",
     "系统总体设计与关键技术预研：五层架构、数据模型（15 张表）、容器沙箱与编排方案、评估工具设计"),
    ("2027.01—2027.02中旬",
     "系统实现与联调：测试 Agent 编排引擎、用例生成与修复方法、容器执行链路、评估与报告工具，达到可运行状态"),
    ("2027.02中旬—2027.02下旬",
     "实验与数据分析（性能对比、链路受控对比、盲测语料控制变量矩阵、变异标定）并完成论文初稿；"
     "第二学期第 1 周接受中期检查（要求系统可运行、提交论文初稿，线下当面检查）"),
    ("2027.03—2027.05",
     "论文二稿（第二学期第 2—5 周）→ 定稿（第 7 周前，截止 2027 年 4 月 9 日）→ "
     "第 8 周配合答辩前论文审查 → 初次答辩（2027 年 4 月 24 日）与二次答辩（2027 年 5 月 12 日）"),
]

t = d.tables[2]
assert t.rows[1].cells[0].text.strip() == "阶段", "T2 不是工作进度表"
for i, (period, content) in enumerate(PLAN, start=2):
    set_cell_text(t.rows[i].cells[1], period)
    set_cell_text(t.rows[i].cells[2], content)

d.save(PATH)
print("saved:", PATH)

# ---- 复核 ----
d2 = Document(PATH)
for row in d2.tables[0].rows:
    for cell in row.cells:
        if "指导时间" in cell.text:
            print("T0 指导时间:", cell.text.split("\n")[0])
            break
    else:
        continue
    break
print()
for i in range(1, 7):
    r = d2.tables[2].rows[i]
    print(f"R{i}: {r.cells[0].text.strip()} | {r.cells[1].text.strip()} | {r.cells[2].text.strip()}")
