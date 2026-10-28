"""按开题会老师意见收敛范围：软件测试 → 单元测试（一次性修订脚本）。

老师意见：软测大类太宽，本科生应聚焦一个具体方向（压测/黑盒/白盒等）。
收敛口径：聚焦**单元测试层级**的自动化生成与修复（Python、功能正确性）；
黑盒设计方法（等价类/边界值/判定表）与白盒覆盖反馈作为单元测试内部的设计技术保留。
"""
from docx import Document

PATH = "docs/开题报告-课题论证-草稿.docx"
OLD_TITLE = "基于大模型 Agent 与容器技术的自动化软件测试方法研究"
NEW_TITLE = "基于大模型 Agent 与容器技术的自动化单元测试方法研究"

d = Document(PATH)

# ---- 1) 题目名称（T0 选定题目栏） ----
hit = 0
for row in d.tables[0].rows:
    for cell in row.cells:
        for p in cell.paragraphs:
            for r in p.runs:
                if OLD_TITLE in r.text:
                    r.text = r.text.replace(OLD_TITLE, NEW_TITLE)
                    hit += 1
print("题目替换处数:", hit)

# ---- 2) P6 选题依据末尾追加范围界定句 ----
cell = d.tables[1].rows[2].cells[0]
p6 = cell.paragraphs[6]
SCOPE = ("需要说明的是，软件测试按层级分为单元、集成、系统、验收测试，按方法分为黑盒与白盒等；"
         "本课题聚焦其中人力投入最密集、自动化收益最明确的单元测试层级"
         "（面向 Python 语言、以功能正确性为目标），"
         "性能、安全、界面等测试类型不在研究范围内；"
         "黑盒设计方法（等价类、边界值、判定表）与白盒覆盖分析作为单元测试用例设计阶段内部使用的技术。")
assert "编排测试全流程" in p6.text
p6.runs[0].text = p6.text + SCOPE
for r in p6.runs[1:]:
    r.text = ""

# ---- 3) P14 预期结果里的系统称谓 ----
p14 = cell.paragraphs[14]
assert "端到端自动化软件测试 Agent 系统" in p14.text
p14.runs[0].text = p14.text.replace("端到端自动化软件测试 Agent 系统",
                                    "端到端自动化单元测试 Agent 系统")
for r in p14.runs[1:]:
    r.text = ""

# ---- 4) 名词说明追加第 13 条：范围界定 ----
last = cell.paragraphs[-1]._p
NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
new_p = __import__("copy").deepcopy(last)
runs = new_p.findall(NS + "r")
for r in runs[1:]:
    new_p.remove(r)
t = runs[0].find(NS + "t")
t.text = ("13. 范围界定：按老师开题会意见收敛——本课题只做单元测试层级的自动化"
          "（Python、功能正确性）；压力测试、安全测试、界面/E2E 测试均不在范围内，"
          "黑盒/白盒指用例设计阶段内部使用的两类方法。")
t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
last.addnext(new_p)

d.save(PATH)
print("saved")

# ---- 复核 ----
d2 = Document(PATH)
c2 = d2.tables[1].rows[2].cells[0]
t0 = d2.tables[0].rows[6].cells[0].text
print("题目栏:", [l for l in t0.split(chr(10)) if "题目名称" in l])
print("P6 末尾:", c2.paragraphs[6].text[-120:])
print("P14 头:", c2.paragraphs[14].text[:60])
print("名词说明末条:", c2.paragraphs[-1].text[:60])
