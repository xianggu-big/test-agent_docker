"""五种用例设计方法的提示词知识库 —— gen_tests Skill 的核心。

设计纪律（论文理论章"测试设计阶段"的落点）：
让 Agent 按方法逐类生成，而不是自由发挥 —— 这是生成正确率的第一道抓手。
方法骨架 = 传统测试理论（等价类/边界值/判定表/覆盖/错误推测）的显式化。
"""

SYSTEM_PROMPT = (
    "你是资深软件测试工程师。严格按给定的用例设计方法生成 pytest 用例，"
    "断言必须先于执行结果确定（预期结果来自需求而非实际输出）。只输出 JSON。"
)

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


def build_user_prompt(sources: list[tuple[str, str]], requirement: str) -> str:
    """sources: [(相对路径, 源码)]；requirement: 用户自然语言需求。"""
    src_text = "\n\n".join(
        f"### 被测文件：{path}\n```python\n{code}\n```" for path, code in sources)
    method_text = "\n".join(f"{i}.【{m['name']}】{m['skeleton']}"
                            for i, m in enumerate(METHODS, 1))
    return f"""## 被测源码
{src_text}

## 用户需求
{requirement or "（无额外需求，按源码行为设计）"}

## 用例设计方法（每种方法至少生成 1 条用例，name 后缀标注方法名）
{method_text}

## 输出格式
只输出一个 JSON 对象，禁止输出任何其他文字或 markdown 代码块标记：
{{"imports": "测试文件顶部所需的全部 import 语句", "cases": [{{"name": "test_xxx_等价类划分", "method": "等价类划分", "code": "def test_xxx_等价类划分():\\n    ..."}}]}}"""
