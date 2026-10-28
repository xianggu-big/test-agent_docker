"""五种用例设计方法的提示词知识库 —— gen_tests Skill 的核心。

设计纪律（论文理论章"测试设计阶段"的落点）：
让 Agent 按方法逐类生成，而不是自由发挥 —— 这是生成正确率的第一道抓手。
方法骨架 = 传统测试理论（等价类/边界值/判定表/覆盖/错误推测）的显式化。
"""

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


def build_plan_prompt(requirement: str) -> str:
    return f"""## 用户需求
{requirement}

## 任务
把需求拆解为可验证的测试条件：每条是单一行为陈述、可通过测试证伪；标注优先级（高/中/低）
与建议的用例设计方法（等价类划分/边界值分析/判定表/语句分支覆盖/错误推测法）。
只拆解需求声明过的行为，不要发明；需求含糊处按最保守语义拆解并在 condition 中注明假设。

## 输出格式
只输出一个 JSON 对象：
{{"conditions": [{{"requirement_ref": "需求原文关键句", "condition": "可验证行为陈述",
   "priority": "高", "method_hint": "等价类划分"}}]}}"""

MAX_SOURCE_CHARS = 8000   # 被测源码总量上限，防 token 爆炸

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


def build_user_prompt(sources: list[tuple[str, str]], requirement: str,
                      coverage_gaps: list[dict] | None = None,
                      coverage_target: float | None = None,
                      conditions: list[dict] | None = None,
                      include_methods: bool = True,
                      boundary_attack: bool = False) -> str:
    """sources: [(相对路径, 源码)]；requirement: 用户自然语言需求。

    coverage_gaps: 上一轮 term-missing 逐文件缺口（①覆盖缺口回灌，CoverUp 模式）——
    有缺口时优先生成命中缺口行的用例，这是"补测轮次知道该往哪补"的信息通道。
    conditions: plan 节点拆解的测试条件（RTM）——用例逐条对准条件设计。
    include_methods: 五方法骨架开关（实验变量 V2；False=自由发挥对照组）。
    boundary_attack: 边界攻击纪律开关（实验变量 V6；True=强制边界外输入用例）。
    """
    src_text = "\n\n".join(
        f"### 被测文件：{path}\n```python\n{code}\n```" for path, code in sources)
    method_text = "\n".join(f"{i}.【{m['name']}】{m['skeleton']}"
                            for i, m in enumerate(METHODS, 1)) if include_methods else ""
    boundary_text = BOUNDARY_ATTACK_SECTION if boundary_attack else ""
    cond_text = ""
    if conditions:
        rows = "\n".join(f"{i}. [{c.get('priority') or '中'}] {c['condition']}"
                         f"（建议方法：{c.get('method_hint') or '不限'}）"
                         for i, c in enumerate(conditions, 1))
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

    return f"""## 被测源码
{src_text}

## 用户需求
{requirement or "（无额外需求，按源码行为设计）"}{cond_text}{gap_text}{boundary_text}

## 用例设计方法（每种方法至少生成 1 条用例，name 后缀标注方法名）
{method_text if include_methods else "（本组不限定方法，按你的测试经验自由设计用例）"}

## 输出格式
只输出一个 JSON 对象，禁止输出任何其他文字或 markdown 代码块标记。
expected = 预期结果（断言依据，必须先于执行确定）；expected_basis = 该预期值的出处，
四选一：「需求明示」「docstring文档」「源码推导」「常识」——预期值不得从实际运行结果反推。
{{"imports": "测试文件顶部所需的全部 import 语句",
  "cases": [{{"name": "test_xxx_等价类划分",
    "method": "等价类划分",
    "priority": "高/中/低",
    "input": "输入条件（来自需求的输入域）",
    "steps": "操作步骤",
    "expected": "预期输出/状态变化",
    "expected_basis": "需求明示",
    "code": "def test_xxx_等价类划分():\\n    ..."}}]}}"""
