"""开题报告 docx 修订脚本（一次性使用，2026-10-28）。

三处内容修订 + 一页名词说明：
1. P6 为 arXiv:2412.14137 补出处（作者/年份/预印本身份）；
2. P8 单元测试数 87 → 100（实测 --collect-only）；
3. P14 预期结果按实测数据重写（剔除 X2 归因错误、补诚实边界）；
4. 追加【名词说明】，把自拟叫法解释成普通话。
"""
from docx import Document
import copy

PATH = "docs/开题报告-课题论证-草稿.docx"

d = Document(PATH)
cell = d.tables[1].rows[2].cells[0]
paras = cell.paragraphs


def set_text(p, text):
    """保留第一个 run 的格式，整段替换文本。"""
    runs = p.runs
    assert runs, "paragraph has no run"
    runs[0].text = text
    for r in runs[1:]:
        r.text = ""


# ---- 1) P6：补 arXiv:2412.14137 出处 ----
old6_marker = "实证研究（arXiv:2412.14137）"
p6 = paras[6]
assert old6_marker in p6.text, p6.text[:120]
set_text(p6, p6.text.replace(
    old6_marker,
    "实证研究（Mathews 与 Nagappan，arXiv:2412.14137，2024 年 12 月，cs.SE 预印本）",
))

# ---- 2) P8：单测数量 87 → 100 ----
p8 = paras[8]
assert "87 项单元测试" in p8.text
set_text(p8, p8.text.replace("通过 87 项单元测试", "通过 100 项单元测试"))

# ---- 3) P14：预期结果按实测数据重写 ----
p14 = paras[14]
assert "0.33～0.47" in p14.text
set_text(p14, (
    "实现一个端到端自动化软件测试 Agent 系统（Web 界面、编排引擎、容器沙箱、实验数据库），"
    "并形成可复现的实验结论："
    "①容器化执行无性能惩罚（同机三次复跑：容器热运行 0.80～0.81 秒，Windows 宿主直跑 3.10～3.11 秒，"
    "约快 3.8 倍；冷启动一次性约 1.3 秒；诚实边界＝单机、微型被测项目、Windows 宿主）；"
    "②测试条件层（plan/RTM）对缺陷检出率呈正向作用的方向性证据：关闭该层时盲测语料检出率 0.20±0.00"
    "（n=3，三次完全一致），开启时 0.33±0.12（n=3）；另有对照格显示关闭“五方法提示词骨架”时检出率为 "
    "0.47±0.12，提示检出能力的主要来源可能是“把需求拆成显式测试条件”而非方法骨架，该现象列入后续实验的解释对象；"
    "上述数字受“每格 n=3、语料含 2 个占位项使检出率上限为 0.6”限制，只作方向性结论、不作定论；"
    "③缺陷证人冻结机制经单元测试锁定，已完成的实验任务中未出现证人被回炉掩盖的情形，"
    "疑似产品缺陷均带证据链如实上报；"
    "④产出“最适合 Agent 测试的方法组合”的量化结论与一套含盲测协议、控制变量矩阵的实验方法论。"
))

# ---- 4) 追加【名词说明】 ----
GLOSSARY = [
    "【名词说明】为便于评阅，先把本文自拟的叫法与通行术语对照如下（不改变上文任何论述）：",
    "1. 五层架构：本系统自上而下分为界面层（Web 操作界面）、编排层（按固定流程调度各步骤）、技能层（把工具封装成可调用、有调用记录的技能）、沙箱层（Docker 容器）、数据层（SQLite 数据库，15 张表）。",
    "2. 固定图编排：主流程用 LangGraph 画成一张固定的有向图，每个步骤是一个“节点”；大模型只在节点内部产出内容（如拆测试条件、写用例），不负责决定流程走向——以保证系统可复现、可画图、可讲解。",
    "3. RTM（需求追踪矩阵）：把“需求→测试条件→用例→执行结果”逐级对应起来的矩阵，用来证明每条需求都有用例覆盖，并在需求变更时反查受影响的用例。",
    "4. R-Loop（补测闭环）：测试失败后按“归因→修复→重测”循环的机制，设轮次上限；本课题另加三道停止条件（准出达标、重复失败熔断、预算耗尽），到点如实上报而不伪装成功。",
    "5. 指纹熔断：把每轮失败的关键报错信息取哈希得到“指纹”，若连续两轮指纹相同，判定为原地打转并停止重试（对应 ISTQB“测试会磨损”原则）。",
    "6. 缺陷证人冻结：若失败被归因为“产品真缺陷”，该失败用例被保留为证据（称为“证人”），禁止通过修改断言让它变绿，也禁止删除。",
    "7. 预期值来源（expected_basis）：在生成断言时标注预期值从哪里来（需求原文／文档注释／从源码推导），用于判断该断言是否可信；来自需求明示的失败用例永不被改写。",
    "8. 准出标准冻结：在测试开始前就把“覆盖率阈值＋轮次预算”固定下来，过程中不得修改，防止事后调低标准。",
    "9. 静态闸与可收集闸：生成的用例先做语法与引用检查（不执行代码），再在容器内试收集（pytest --collect-only），两道都通过才允许真正执行。",
    "10. 变异标定：人为向被测程序注入已知的小错误（变异体），看系统能否把它正确判为“产品缺陷”；因为真值已知，可用混淆矩阵算出归因准确率。",
    "11. 三方盲测：出题人（构造含已知缺陷的语料并保留答案键）、答题人（被测评的 Agent 系统）、阅卷人（评分脚本）三方隔离，答题方看不到答案键。",
    "12. 控制变量矩阵（X 格）：每次只关闭一个机制、其余配置不变地重复运行，用检出率（recall＝检出的植入缺陷数／植入缺陷总数）比较各机制的贡献。",
]

template = paras[14]._p
last = cell.paragraphs[-1]._p
for text in GLOSSARY:
    new_p = copy.deepcopy(template)
    # 清掉除第一个 run 外的全部 run
    runs = new_p.findall(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
    for r in runs[1:]:
        new_p.remove(r)
    # 写入文本
    t = runs[0].find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    last.addnext(new_p)
    last = new_p

d.save(PATH)
print("saved:", PATH)

# 复核
d2 = Document(PATH)
c2 = d2.tables[1].rows[2].cells[0]
print("paragraphs now:", len(c2.paragraphs))
for i, p in enumerate(c2.paragraphs):
    if i in (6, 8, 14):
        print(f"--- P{i}:", p.text[:150])
print("--- last:", c2.paragraphs[-1].text[:80])
