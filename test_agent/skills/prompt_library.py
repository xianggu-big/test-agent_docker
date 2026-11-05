"""五种用例设计方法的提示词知识库 —— gen_tests Skill 的核心。

设计纪律（论文理论章"测试设计阶段"的落点）：
让 Agent 按方法逐类生成，而不是自由发挥 —— 这是生成正确率的第一道抓手。
方法骨架 = 传统测试理论（等价类/边界值/判定表/覆盖/错误推测）的显式化。
"""
import re

SYSTEM_PROMPT = (
    "你是资深软件测试工程师。严格按给定的用例设计方法生成 pytest 用例，"
    "断言必须先于执行结果确定（预期结果来自需求而非实际输出）。只输出 JSON。"
)

PLAN_SYSTEM = (
    "你是资深软件测试分析师（测试分析阶段）。把用户的自然语言需求拆解为可验证的测试条件"
    "（需求追踪矩阵 RTM 的条件层）。只输出 JSON。"
)

REPORT_SYSTEM = (
    "你是测试报告撰写员（测试评估阶段）。报告的数字只能来自输入数据，禁止出现输入之外的任何数字；"
    "提及通过数、失败数、覆盖率时使用固定表述「通过 N」「失败 N」「跳过 N」「覆盖率 P%」。只输出 JSON。"
)

REVIEW_SYSTEM = (
    "你是测试报告审查员（报告的二次审查）。对照原始数据审查报告叙述是否一致、结论是否有证据。"
    "只输出 JSON。"
)

CROSS_REVIEW_SYSTEM = (
    "你是测试用例审查员（独立于生成方的二次审查）。逐条审查测试用例的断言是否与用户需求对应："
    "预期值是否来自需求语义而非迁就实现反推、断言是否具备证伪能力。只依据给定信息判断，"
    "不要臆测信息之外的意图；判不准的条目给 pass 并注明不确定——宁可放行，不可误杀。只输出 JSON。"
)

# —— AI 代审角色分离（docs/AI代审设计-20261031.md 红线：代审提示词 ≠ 生成提示词，
# 与 V9 同源；三份独立系统提示词 = 同模型角色分离口径的根基） ——
D1_SYSTEM = (
    "你是测试准入审查员（AI 代审模式，独立于任务创建者与用例生成方）。"
    "评估该测试任务是否值得投入执行：需求是否可测、风险点在哪、预期收益。"
    "审查意见必须像人写评审意见一样给出具体理由。只输出 JSON。"
)

REVIEWER_SYSTEM = (
    "你是测试用例集审查员（AI 代审模式，独立于用例生成方，代行人工审查职责）。"
    "逐条审查用例集：是否覆盖测试条件、断言是否有证伪能力、有无重复或迁就实现的嫌疑；"
    "对缺口提出增补意见。每个决策必须写明理由并引用证据（条件编号/断言类型/覆盖缺口���，"
    "像人写审查意见一样可追责。宁可放行存疑项标注理由，不可无理由删改。只输出 JSON。"
)

DEFECT_ANALYST_SYSTEM = (
    "你是缺陷分析师（测试评估阶段，报告概览的失败分析）。只依据给定的失败消息、用例预期、"
    "被测源码与需求做分析：根因是推测，必须引用源码中的具体函数或失败消息原文；"
    "失败既可能是产品缺陷也可能是用例预期写错（oracle 问题），两种都要如实判别并给对应修法。"
    "禁止编造证据之外的问题，禁止泛化建议。只输出 JSON。"
)


def build_map_prompt(conditions: list[dict], cases: list[dict]) -> str:
    """map_requirements 的 LLM 辅助映射：条件 ↔ 用例 关联判定。"""
    cond_rows = "\n".join(f"{c['id']}. {c['condition']}" for c in conditions)
    case_rows = "\n".join(f"- {c['name']}（预期：{(c.get('expected') or '')[:60]}）"
                          for c in cases)
    return f"""## 测试条件（RTM 条件层）
{cond_rows}

## 已生成的测试用例
{case_rows}

## 任务
判断每条测试条件被哪些用例覆盖（用例名精确匹配上面列表）。某条件没有任何用例覆盖即为缺口（gap）。
只依据行为语义对应关系判断，不要发明用例名。

## 输出格式
{{"mappings": [{{"condition_id": 1, "cases": ["test_xxx"]}}]}}"""


def build_cross_review_prompt(requirement: str, cases: list[dict]) -> str:
    """交叉语义审（§3.6 第 3 道闸）：审查者角色逐案审断言是否对应需求。"""
    rows = "\n".join(
        f"- {c['name']}｜预期：{(c.get('expected') or '未标注')[:80]}"
        f"｜预期依据：{(c.get('expected_basis') or '未标注')[:60]}\n"
        f"  代码：{c['code'][:400]}"
        for c in cases)
    return f"""## 用户需求
{requirement}

## 待审查的测试用例
{rows}

## 审查任务（逐条判定）
1. 断言的预期值是否对应需求声明的行为（而非从实现反推的迁就值）
2. 断言是否具备证伪能力（具体值/关系比较，而非裸真值断言）
3. 用例的输入与所断言的行为是否一致
name 必须精确取自上面列表。

## 输出格式
{{"reviews": [{{"name": "test_xxx", "verdict": "pass", "reason": "一句话依据"}}]}}"""


def build_regen_prompt(sources: list[tuple[str, str]], requirement: str,
                       case: dict, reason: str) -> str:
    """单案重造（交叉审 fail 后的定向修复）：原案 + 否决理由 + 被测源码 → 重造这一条。"""
    src_text = "\n\n".join(f"### {p}\n{t}" for p, t in sources)
    return f"""## 用户需求
{requirement}

## 被测源码
{src_text}

## 被审查否决的用例（只重造这一条）
名称：{case['name']}
代码：
{case['code']}

## 审查否决理由
{reason}

## 重造任务
针对同一测试意图重新设计这一条用例：断言的预期值必须来自需求语义（不得迁就实现反推）、
具备证伪能力（具体值/关系比较），并修正审查指出的具体问题。
只输出一个 JSON 对象，"cases" 数组内只放这 1 条用例。

## 输出格式
{{"imports": "", "cases": [{{"name": "test_xxx", "method": "边界值分析", "code": "def test_xxx():\\n    ..."}}]}}"""


def build_plan_prompt(requirement: str, hotspots: list[dict] | None = None) -> str:
    """hotspots: W4 圈复杂度热点（radon 实测）——控制流密集函数提示设计方法倾斜。"""
    hot_text = ""
    if hotspots:
        rows = "\n".join(f"- {h['file']}::{h['name']}（圈复杂度 {h['complexity']}，rank {h['rank']}）"
                         for h in hotspots)
        hot_text = f"""

## 圈复杂度热点（W4 实测，优先覆盖）
以下函数控制流密集（基路径多、缺陷高发），为其拆解的条件建议标注方法"语句分支覆盖"，
并保证真假分支方向都有条件覆盖：
{rows}"""
    return f"""## 用户需求
{requirement}
{hot_text}
## 任务
把需求拆解为可验证的测试条件：每条是单一行为陈述、可通过测试证伪；标注优先级（高/中/低）
与建议的用例设计方法（等价类划分/边界值分析/判定表/语句分支覆盖/错误推测法）。
只拆解需求声明过的行为，不要发明；需求含糊处按最保守语义拆解并在 condition 中注明假设。

## 输出格式
只输出一个 JSON 对象：
{{"conditions": [{{"requirement_ref": "需求原文关键句", "condition": "可验证行为陈述",
   "priority": "高", "method_hint": "等价类划分"}}]}}"""

MAX_SOURCE_CHARS = 24000  # 被测源码总量上限，防 token 爆炸（24000 字符≈8k token，DeepSeek 64k 上下文内留足生成余量；2026-10-30 H1 实锤 8000 时真实项目只喂到 setup.py，见 docs/真实项目跑测/20261030-H1-python-titlecase-基线.md）

METHODS = [
    {"name": "等价类划分",
     "skeleton": "将输入域划分为有效/无效等价类，每类取一个代表值：有效类验证正常行为，"
                 "无效类验证异常处理（空值、错误类型、超范围值）。"},
    {"name": "边界值分析",
     "skeleton": "围绕数值/集合边界取 min-1、min、min+1、max-1、max、max+1，"
                 "以及空集合、单元素集合——边界是缺陷高发区。"},
    {"name": "判定表",
     "skeleton": "识别多条件组合逻辑，列出条件全组合（或化简后的主要组合），"
                 "每个组合一条用例，说明预期结果。"},
    {"name": "语句分支覆盖",
     "skeleton": "阅读控制流，确保每条语句至少执行一次、每个 if/循环 的真假分支各至少"
                 "执行一次；确实无法覆盖的分支在 name 中注明 uncovered。"},
    {"name": "错误推测法",
     "skeleton": "基于常见缺陷模式做攻击性设计：None、空串、0、负数、极大值、类型不匹配、"
                 "除零、重复调用、非法调用顺序、浮点精度。"},
]


BOUNDARY_ATTACK_SECTION = """

## 边界攻击纪律（强制，实验变量 V6）
对每个被测公开函数至少生成 1 条边界外输入用例：min-1、max+1、空串/None、0/负数、
超长输入等越界值，断言其行为符合需求契约——预期值必须来自需求而非实际输出，
禁止"实现返回什么就断言什么"。"""

PBT_SECTION = """

## 属性测试（第七法，实验变量 PBT）
除常规示例外，再生成至少 1 条基于 hypothesis 的属性测试：用 @given(st...) 把输入域声明为
策略而非手选样例，属性函数表述"对任意合法输入都应成立的不变量"（如幂等/往返一致、
单调性、输出格式不变量、与朴素参照实现等价），settings(max_examples=20) 限制采样数。
属性与预期同样必须先于执行确定（来自需求或数学性质，禁止从实际输出反推）。
断言用具体关系比较（==、数值/格式恒等式），禁止 is not None / 裸真值这类弱断言。骨架：
@given(st.integers(min_value=0, max_value=10**6))
def test_xxx_属性测试(cents):
    text = format_amount(cents)
    assert len(text.split(".")[1]) == 2   # 不变量示例：小数部分恒两位"""

# —— P2 反惯例正例锚定（docs/防御矩阵-20261105：对照示例是强信号，负面清单是弱信号；
# 覆盖 A9 幻觉 import / A11 弱断言 / A12 示例照抄三组）——
CONTRAST_SECTION = """

## 正误对照（每条纪律给正确写法 ✅ 与错误写法 ❌——❌ 形态会被静态闸整案剔除）
✅ import 只引「被测源码」里出现过的模块：
   from calc import check_length          （calc.py 被测源码里存在）
❌ 凭记忆引未见过的包：
   import bleach, validators              （被测源码/requirements 均未出现→整案剔除）
✅ 断言是具体关系比较：
   assert check_length("abcd1234") is False
   assert quote_total(2, 3) == 150
❌ 弱断言（无证伪能力）：
   assert result is not None
   assert True
❌ 把示例里的占位值当真实数据照抄（示例 room_id={room_id} 是占位符）：
   client.post("/rooms", json={"room_id": "{room_id}"})   ← ❌ 原样照抄
✅ 占位符由你构造合法值：
   client.post("/rooms", json={"room_id": 1})
"""

# —— P1 环境契约统一段（docs/防御矩阵-20261105：整合散落纪律+新增 6/7 两条；
# {max_cases} 由调用方按条件数动态填）——
ENV_CONTRACT_SECTION = """

## 执行环境契约（必须遵守，违反=整案作废）
1. 用例文件写在项目根目录（不是 test/ 子目录）：__dirname 或 __file__ 所在目录即项目根，
   禁止 path.join(__dirname, '..') / Path(__file__).parent.parent 等上跳写法。
2. 只允许 import 在「被测源码」中出现过的模块与符号，及 pytest/标准库；禁止假设任何
   未列出的依赖已安装。
3. 状态码与响应 schema 以「API 行为文档」为准；业务字段名以 spec 为准，禁止自拟。
4. 本轮最多生成 {max_cases} 条用例（超出部分会被丢弃，宁精勿滥）。
5. 整体响应永远是最外层 {{"imports": ..., "cases": [...]}}；template+slots 只是元素内字段。
6. 禁止 @pytest.mark.parametrize 等参数化装饰器（单条用例=单个测试函数；参数化会被
   静态闸整案剔除）。
7. 禁止 mock 被测模块本身（mock.patch("被测模块.函数") / monkeypatch.setattr(被测模块, …)
   会被静态闸整案剔除）；仅允许 mock 网络/文件系统/时钟等外部依赖。
"""

TEMPLATE_SECTION = """

## 槽位模板通道（【等价类划分】【边界值分析】【错误推测法】的基础用例优先走此通道）
这三类方法的用例形态固定，用"模板+槽位"代替完整代码（更省时、通过率更高）：
cases 元素用 template + slots 代替 code，其余字段（name/method/priority/input/steps/
expected/expected_basis）照常填写。两种形态可混用：
{"name": "test_div_边界值", "method": "边界值分析", "priority": "高",
 "input": "除数与被除数边界", "steps": "调用 div", "expected": "见各点预期",
 "expected_basis": "需求明示", "template": "T2",
 "slots": {"mode": "valid", "imports": "from calc import div",
   "target": "div({v}, 2)",
   "cases": [{"value": 1, "expected": 0, "label": "min"},
              {"value": 2, "expected": 1, "label": "min+1"}]}}
- slots.mode="valid"：cases 元素 {"value": 字面量, "expected": 字面量, "label": "说明"}；
  slots.mode="invalid"：元素 {"value": 字面量, "error": "内置异常名", "label": "说明"}，
  异常路径必须单发一条 mode=invalid 的模板用例（一条用例只产一个测试函数）。
- target 是含 {v} 占位符的调用表达式；T2 的 valid 槽位至少 4 个点
  （min-1/min/min+1/max-1/max/max+1 语义，边界是缺陷高发区，禁止省略）；
  T1 等价类有效与无效各至少 2 行（无效走 mode=invalid 单发）；T5 至少 2 条攻击用例。
- 【格式铁律】整体响应永远是最外层 {"imports": ..., "cases": [元素...]}；
  template+slots 只是 cases 元素内部的字段，严禁把槽位对象直接作为顶层响应。
- 预期值同样必须先于执行确定、标注 expected_basis；槽位非法的用例会被整案剔除。"""

STATE_TRANSITION_SECTION = """

## 状态迁移法（扩展方法，实验开关）
若需求描述了状态机语义（状态/事件/迁移，如订单、会话、审批流），用模板 T4 生成迁移覆盖用例：
{"name": "test_order_状态迁移", "method": "状态迁移法", "priority": "高",
 "input": "订单状态机", "steps": "走合法迁移并拒绝非法迁移", "expected": "状态按迁移表变化",
 "expected_basis": "需求明示", "template": "T4",
 "slots": {"imports": "from order import Order",
   "setup": "order = Order('pending')",
   "steps": [{"call": "order.pay()", "assert": "order.status == 'paid'"},
              {"call": "order.ship()", "assert": "order.status == 'shipped'"}],
   "invalid": [{"call": "order.pay()", "error": "RuntimeError"}]}}
- 合法迁移必须全覆盖（每条迁移一个 call+assert 对）；非法迁移写入 invalid（须被拒绝）。
- 断言的是需求声明的状态语义，禁止从实际输出反推；与需求无关的状态机不要发明。"""

METAMORPHIC_SECTION = """

## 蜕变测试（扩展方法，实验开关；Chen et al., ACM CSUR 2018）
对预期值难以显式给出（无明确 oracle）的行为，先从需求/数学性质提炼蜕变关系（MR），
再生成"源输入 + 跟随输入 + 关系断言"用例。常见 MR 模式（按需选用，禁止硬凑）：
- 幂等：f(f(x)) == f(x)（归一化/格式化类）
- 对称/交换：f(a, b) == f(b, a)（无序比较类）
- 单调：a <= b ⟹ f(a) <= f(b)（排序/评分类）
- 往返：g(f(x)) == x，f 为编码 g 为解码（编解码/序列化类）
- 无关变换不变：f(x) == f(transform(x))，transform 为空白/顺序/大小写等无关扰动
断言的是关系而非具体值；MR 必须来自需求语义或数学性质并标注 expected_basis，
禁止"实现两次调用恰好相等"这类从实现反推的伪 MR。"""


# —— B 源·语言×类型陷阱库（docs/领域知识注入调研-20261031.md：curated 清单=文献空白点，
# 对应资深测试人员经验；发现二前车之鉴——很可能对检出无贡献，消融负结果如实写） ——
PITFALL_KB: dict[str, list[dict]] = {
    "python": [
        {"pattern": "浮点数相等比较", "why": "IEEE754 二进制浮点无法精确表示多数十进制小数",
         "test_hint": "用 abs(a-b)<eps 或 pytest.approx 断言，禁止 ==", "keyword": "float|浮点|小数|比例|折扣|利率"},
        {"pattern": "可变默认参数", "why": "def f(x=[]) 的列表跨调用共享，状态跨调用泄漏",
         "test_hint": "同一默认参数连续调用两次，断言第二次不受第一次影响", "keyword": "默认参数|cache|缓存|累积"},
        {"pattern": "字符串编码/规范化", "why": "大小写/全半角/Unicode 归一化差异（如 ß 与 ss、土耳其 ı）",
         "test_hint": "用含非 ASCII、大小写混合的输入断言归一化行为", "keyword": "编码|normalize|大小写|title|upper|unicode"},
        {"pattern": "边界含不含", "why": "range/切片/比较的边界语义（<= 与 <、[a,b) 与 [a,b]）最易错",
         "test_hint": "对每个数值边界取 min-1/min/max/max+1 四点断言", "keyword": "边界|范围|上限|下限|超过|至少|至多"},
        {"pattern": "None 与空容器混用", "why": "None、''、[]、{} 的真值语义不同，函数常只处理其一",
         "test_hint": "None/空串/空列表/空字典分别单发用例", "keyword": "空|None|缺省|可选"},
        {"pattern": "异常吞没", "why": "except 后返回默认值会把错误静默洗成正常返回",
         "test_hint": "构造非法输入断言异常类型，而非断言默认返回值", "keyword": "异常|错误|失败|重试|回退"},
        {"pattern": "整数除法与舍入", "why": "/ 与 //、round 的银行家舍入（round(0.5)=0）与直觉不符",
         "test_hint": "对 .5/.25 等中点值断言舍入方向", "keyword": "除|平均|舍入|round|取整"},
        {"pattern": "时区与本地时间", "why": "datetime.now() 依赖本地时区，跨时区/夏令时行为漂移",
         "test_hint": "用固定 aware datetime 注入，断言 UTC 转换", "keyword": "时间|日期|时区|timestamp|天|月"},
    ],
    "web": [
        {"pattern": "并发幂等", "why": "重复提交/并发请求造成重复创建或双扣",
         "test_hint": "同幂等键重复调用两次，断言只生效一次", "keyword": "下单|支付|提交|创建|并发"},
        {"pattern": "时区序列化", "why": "API 返回时间缺时区标注，客户端解析漂移一天",
         "test_hint": "断言响应时间的 ISO8601 带时区偏移", "keyword": "时间|创建时间|deadline"},
        {"pattern": "缓存穿透", "why": "不存在的 key 反复打穿缓存击穿数据库",
         "test_hint": "查询不存在资源两次，断言第二次不再触发底层", "keyword": "缓存|查询|详情|cache"},
        {"pattern": "分页边界", "why": "offset 越界返回空页还是报错、last page 判定",
         "test_hint": "请求超过末页的 page，断言空列表而非 5xx", "keyword": "分页|列表|page|limit|offset"},
        {"pattern": "输入归一化", "why": "前后空格/大小写/全半角在唯一性校验时口径不一",
         "test_hint": "带空格与不带空格的等价输入，断言判定一致", "keyword": "登录|注册|唯一|重复|trim"},
        {"pattern": "部分失败原子性", "why": "批量操作中途失败留下半成品状态",
         "test_hint": "构造第二条必然失败的批处理，断言无残留副作用", "keyword": "批量|事务|导入|上传"},
    ],
    "cli": [
        {"pattern": "退出码约定", "why": "成功 0/失败非 0 是脚本编排的契约，异常路径常漏",
         "test_hint": "断言非法输入的退出码非 0 且 stderr 有信息", "keyword": "命令|cli|脚本|退出"},
        {"pattern": "信号中断清理", "why": "SIGINT/SIGTERM 后临时文件/锁未清理",
         "test_hint": "中断长任务后断言无残留临时文件", "keyword": "中断|清理|临时|锁"},
        {"pattern": "stdin/stdout 编码", "why": "管道二进制数据/非 UTF-8 环境的解码崩溃",
         "test_hint": "用非 ASCII 输入走管道断言不崩", "keyword": "输入|输出|编码|管道"},
        {"pattern": "参数组合冲突", "why": "互斥 flag 同给、缺必需参数的报错质量",
         "test_hint": "互斥参数同给，断言明确报错而非静默择一", "keyword": "参数|选项|flag|开关"},
    ],
}


def select_pitfalls(lang: str, requirement: str, top: int = 4) -> list[dict]:
    """按 analyze 主语言 + 需求关键词选陷阱子集（确定性、零 LLM）：需求像 Web/CLI 用对应清单，
    其余按语言回退 python；条目数封顶防提示词膨胀。"""
    req = requirement or ""
    if re.search(r"API|接口|HTTP|REST|FastAPI|Flask|路由|请求|响应", req, re.IGNORECASE):
        pool = PITFALL_KB["web"]
    elif re.search(r"命令行|CLI|argparse|终端|脚本参数", req, re.IGNORECASE):
        pool = PITFALL_KB["cli"]
    else:
        pool = PITFALL_KB.get(lang or "python") or PITFALL_KB["python"]
    return pool[:top]


def build_pitfall_section(lang: str, requirement: str) -> str:
    """B 源注入段：并进「用例设计方法」之后；命中条目时预期依据须有实质来源。"""
    items = select_pitfalls(lang, requirement)
    if not items:
        return ""
    rows = "\n".join(f"- 【{p['pattern']}】{p['why']} → 测试手法：{p['test_hint']}"
                     for p in items)
    return f"""

## 领域陷阱清单（B 源·错误推测法扩展，实验变量 X9）
资深测试人员在本类目标上的高发缺陷模式（供错误推测法聚焦，禁止硬凑与需求无关的条目）：
{rows}
命中陷阱条目的用例，expected_basis 必须标注实质来源（「需求明示」/「docstring文档」/「源码推导」），
禁止一律标「常识」——依据可溯是审查与归因的前提。"""


def build_user_prompt(sources: list[tuple[str, str]], requirement: str,
                      coverage_gaps: list[dict] | None = None,
                      coverage_target: float | None = None,
                      conditions: list[dict] | None = None,
                      include_methods: bool = True,
                      boundary_attack: bool = False,
                      pbt: bool = False,
                      templates: bool = False,
                      state_transition: bool = False,
                      metamorphic: bool = False,
                      cond_forced: bool = False,
                      existing_cases: list[str] | None = None,
                      api_docs: str | None = None,
                      usage_examples: str | None = None,
                      feedback_text: str | None = None,
                      pitfalls: bool = False,
                      module_lang: str = "",
                      target_language: str = "",
                      seed_example: str | None = None,
                      defense_v2: bool = False) -> str:
    """sources: [(相对路径, 源码)]；requirement: 用户自然语言需求。

    coverage_gaps: 上一轮 term-missing 逐文件缺口（①覆盖缺口回灌，CoverUp 模式）——
    有缺口时优先生成命中缺口行的用例，这是"补测轮次知道该往哪补"的信息通道。
    conditions: plan 节点拆解的测试条件（RTM）——用例逐条对准条件设计。
    include_methods: 五方法骨架开关（实验变量 V2；False=自由发挥对照组）。
    boundary_attack: 边界攻击纪律开关（实验变量 V6；True=强制边界外输入用例）。
    pbt: 属性测试第七法开关（方案B；True=追加 hypothesis 属性测试骨架段）。
    templates: 槽位模板通道开关（方法组合 T1/T2/T5；True=基础方法优先槽位填充）。
    注意消融效度：做"模板 vs 自由生成"对照时对照组必须 templates=False，
    且本函数不向自由通道泄漏任何模板示例（writing-for-agents 教训）。
    state_transition: B4 状态迁移法开关（T4 迁移清单模板；True=追加状态机段）。
    metamorphic: B8 蜕变法开关（MR 模式库提示词；True=追加蜕变关系段）。
    cond_forced: 条件缺口回灌模式（方案C；True=conditions 段改为"强制逐条覆盖"措辞）。
    usage_examples: A 源·repo 校准（调用形态锚点，断言已剥离——只示范"怎么调用"，
    不是预期值来源；预期仍以需求/docstring 为准，防止对齐实现现状的迁就）。
    feedback_text: 两阶段评审的对话增补目标（人工/AI 审查反馈；只增补不动已批准用例）。
    pitfalls: B 源·陷阱清单开关（错误推测法扩展段，按 module_lang+需求选条目）。
    defense_v2: 防御矩阵 v2（P1 环境契约统一段+P2 反惯例正例锚定；docs/防御矩阵
    -prompt与非prompt措施-20261105.md）。False=不注入（提示词逐字节不变，基线保护）。
    max_cases 按 conditions 数动态填（上限 15 与 generate_tests 降采样上限同源）。
    """
    defense_text = ""
    if defense_v2:
        max_cases = max(1, min(len(conditions or []), 15))
        defense_text = (ENV_CONTRACT_SECTION.format(max_cases=max_cases)
                        + CONTRAST_SECTION)
    src_text = "\n\n".join(
        f"### 被测文件：{path}\n```python\n{code}\n```" for path, code in sources)
    seed_text = ""
    if seed_example:
        seed_text = f"""

## 结构样板（本项目容器内已验证可收集的一条用例——严格模仿它）
以下是一条在本项目环境「实测可运行」的用例全文：它的 import 方式、函数包装、断言风格
在本环境成立。批量生成时**逐条严格模仿此结构**，仅替换测试内容与断言对象；禁止引入
此样板未使用的 import 方式、装饰器或目录假设。
```python
{seed_example}
```"""
    docs_text = ""
    if api_docs:
        docs_text = f"""

## API 行为文档（docstring grounding，实验变量 C）
以下是被测函数的官方文档摘录——这是行为规范的第一来源：
预期值与 docstring 冲突时，**以 docstring 为准**，expected_basis 标「docstring文档」；
docstring 没写的行为，按需求文档推断，禁止猜。
{api_docs}"""
    usage_text = ""
    if usage_examples:
        usage_text = f"""

## 仓库自带测试的调用用法（A 源·调用形态锚点，实验变量 X-A）
以下摘自仓库自带测试的**调用形态**（断言已剥离）——仅示范"怎么正确调用"：
参数形态、import 方式、异常触发方式。它们**不是预期值来源**：
预期值一律以需求与 docstring 为准，禁止对齐这些用法背后的实现现状。
{usage_examples}"""
    feedback_sec = ""
    if feedback_text:
        feedback_sec = f"""

## 审查反馈（本轮增补目标——人工/AI 审查员的意见）
{feedback_text}

### 增补纪律（两阶段评审对话，违反即废案）
1. 只**新增**用例：已批准用例不在你的输出里，也禁止试图撤销/重写它们；
2. 禁止与已有用例重复覆盖同一行为（已有用例名单见下方，重复产出会被剔除）；
3. 预期值依据照旧标注 expected_basis：能在需求原文直接定位的标「需求明示」；
   直接来自本条反馈但需求未提及的，诚实标「用户反馈」（不得洗成需求明示）。"""
    method_text = "\n".join(f"{i}.【{m['name']}】{m['skeleton']}"
                            for i, m in enumerate(METHODS, 1)) if include_methods else ""
    boundary_text = BOUNDARY_ATTACK_SECTION if boundary_attack else ""
    pbt_text = PBT_SECTION if pbt else ""
    template_text = TEMPLATE_SECTION if templates else ""
    st_text = STATE_TRANSITION_SECTION if state_transition else ""
    mt_text = METAMORPHIC_SECTION if metamorphic else ""
    pitfall_text = build_pitfall_section(module_lang, requirement) if pitfalls else ""
    cond_text = ""
    if conditions:
        rows = "\n".join(f"{i}. [{c.get('priority') or '中'}] {c['condition']}"
                         f"（建议方法：{c.get('method_hint') or '不限'}）"
                         for i, c in enumerate(conditions, 1))
        if cond_forced:
            have = ""
            if existing_cases:
                have = "\n已有用例（禁止重复，可直接跳过这些已覆盖行为）：" + \
                       "、".join(existing_cases[:40])
            cond_text = f"""

## 未覆盖测试条件（强制逐条覆盖）
以下条件经确定性映射确认尚无用例对准，每条至少生成 1 条对准用例（name 中注明条件序号），
不得与已有用例重复：
{rows}{have}"""
        else:
            cond_text = f"""

## 测试条件（RTM，逐条设计用例对准覆盖）
{rows}"""
    gap_text = ""
    if coverage_gaps:
        rows = "\n".join(f"- {g['file']}: 未覆盖行 {g['missing']}（当前行覆盖 {g['cover']}%）"
                         for g in coverage_gaps if g.get("missing"))
        if rows:
            tgt = f"（准出标准 {coverage_target:.0%}）" if coverage_target else ""
            gap_text = f"""

## 上一轮覆盖缺口（本轮优先补测目标{tgt}）
{rows}
生成用例时必须优先命中上述未覆盖行；已在上一轮覆盖的分支不必重复设计。"""

    basis_note = ("五选一：「需求明示」「docstring文档」「源码推导」「常识」「模型推断」"
                  "（对话增补轮另有第六项「用户反馈」：预期直接来自审查反馈但需求原文无依据时诚实标注）——"
                  if feedback_text else
                  "五选一：「需求明示」「docstring文档」「源码推导」「常识」「模型推断」——")
    # 目标语言输出形态（JS/TS 执行链路 Tier2）：jest 用例名写成 test_xxx（无空格），
    # 与 Python nodeid 口径一致——归因/剪枝/证人冻结按 name 反查全部复用
    is_js = target_language in ("javascript", "typescript")
    if is_js:
        lang_rules = """
语言：JavaScript（jest）。每条用例是一个顶层 test("test_xxx_方法名", () => {...})，
用例名必须以 test_ 开头、**不含空格**；被测模块用 require("./相对路径")（路径以被测文件
相对工作区根目录为准，去掉扩展名）；断言用 expect(...).toBe/toEqual/toThrow 等具体比较，
禁止只写 expect(x).toBeDefined() 这类弱断言。imports 字段放文件顶部的 require 语句。
执行布局（重要）：用例文件将被直接写在项目根目录下（__dirname 即项目根），不会放进
test/ 子目录——引用同目录文件一律 path.join(__dirname, "xxx.js")；**禁止写
path.join(__dirname, "..")**（会指向项目外，必然 Cannot find module）。用
child_process 启动项目内脚本时：cwd 用 __dirname、env 用
Object.assign({}, process.env, extra)（勿整体替换父环境，否则 PATH 丢失）。"""
        code_example = 'test("test_xxx_等价类划分", () => {\\n  expect(fn(1)).toBe(2);\\n});'
        imports_hint = "测试文件顶部所需的全部 require 语句"
    else:
        lang_rules = """
执行布局：用例文件将被直接写在项目根目录下（与被测模块同目录），不会放进子目录——
subprocess/cwd/文件路径的基准用 os.path.dirname(os.path.abspath(__file__))，勿假设
用例位于 test/ 子目录或上跳目录。"""
        code_example = "def test_xxx_等价类划分():\\n    ..."
        imports_hint = "测试文件顶部所需的全部 import 语句"
    return f"""## 被测源码
{src_text}

## 用户需求
{requirement or "（无额外需求，按源码行为设计）"}{cond_text}{seed_text}{docs_text}{usage_text}{feedback_sec}{gap_text}{boundary_text}{pbt_text}{template_text}{st_text}{mt_text}

## 用例设计方法（每种方法至少生成 1 条用例，name 后缀标注方法名）
{method_text if include_methods else "（本组不限定方法，按你的测试经验自由设计用例）"}{pitfall_text}{defense_text}

## 输出格式
只输出一个 JSON 对象，禁止输出任何其他文字或 markdown 代码块标记。{lang_rules}
expected = 预期结果（断言依据，必须先于执行确定）；expected_basis = 该预期值的出处，
{basis_note}
预期值不得从实际运行结果反推。
「需求明示」仅限预期值能在需求原文中直接找到依据时使用；凭你的知识/经验推断的一律
标「源码推导」「常识」或「模型推断」（系统会对「需求明示」做需求重叠确定性校验，
无依据的假标注会被降级为「模型推断」并留痕——错误标注会污染归因）。
{{"imports": "{imports_hint}",
  "cases": [{{"name": "test_xxx_等价类划分",
    "method": "等价类划分",
    "priority": "高/中/低",
    "input": "输入条件（来自需求的输入域）",
    "steps": "操作步骤",
    "expected": "预期输出/状态变化",
    "expected_basis": "需求明示（仅当预期可在需求原文中直接定位；推断而来用「源码推导」/「常识」/「模型推断」)",
    "code": "{code_example}"}}]}}"""
