"""语料生成（无状态出题人路线）—— 子代理并发受限时的替代出题通道。

隔离性质：每次出题是一次全新 LLM 对话，prompt 只含"出题人角色+主题"，无任何
Test Agent 上下文 → 答题链路的输入里没有 answer key（核心隔离性质保持）。
诚实边界：出题/答题同为 DeepSeek（同源模型，docs/链路方法测试文档.md §1 已声明）。

用法：python scripts/gen_corpus_llm.py corpus-001 密码强度校验器
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402

PROMPT = """你是一名独立的软件测试实验"出题人"。你不知道评分方如何工作。

主题：{theme}

生成一份盲测语料，一次性输出一个 JSON 对象（不要 markdown 代码块标记）：
{{
 "requirement_md": "自然语言需求文档全文（Markdown，这是测试方唯一的需求输入）。写清行为契约与边界声明（这是缺陷检测锚点），不要提示哪里有缺陷。",
 "module_name": "模块文件名（如 checker.py）",
 "module_code": "模块全文（60~120 行、3~5 个公开函数、纯标准库）。按需求契约实现但植入至少 4 个（4~5 个）真实行为缺陷，planted_defects 数组长度必须 >=4（边界差一/条件写反/错误处理缺失/计算错位），每个缺陷都能被一个 pytest 断言用例暴露；其余功能保持正确；代码中不得有任何泄题注释；要像正常开发产物。",
 "ground_truth": {{"corpus_id": "{corpus_id}", "theme": "{theme}", "requirement_summary": "一句话",
   "planted_defects": [{{"id": "D1", "type": "boundary_off_by_one", "location": "模块名::函数名",
     "description": "具体到可与疑似缺陷报告客观比对：哪个函数、需求说应怎样、实现实际怎样",
     "expected_assertion": "测试应断言什么输入得到什么输出（对比实现实际输出）",
     "detectable_by": "assertion"}}],
   "notes": "出题协议：缺陷全部为可通过 pytest 断言暴露的行为缺陷"}}
}}
type 只能选：boundary_off_by_one / condition_inverted / missing_error_handling / calculation_error / requirement_misread"""


def main() -> int:
    corpus_id, theme = sys.argv[1], sys.argv[2]
    api_key = sys.argv[3] if len(sys.argv) > 3 else None or __import__("os").environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("需要 DEEPSEEK_API_KEY")
        return 1
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL, temperature=0.8, timeout=180,
                     max_retries=2)
    raw = llm.invoke([("user", PROMPT.format(corpus_id=corpus_id, theme=theme))]).content
    import re
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        print("出题输出不可解析（诚实失败，不硬造语料）")
        return 1
    data = json.loads(m.group(0))
    root = Path(__file__).resolve().parent.parent / "data" / "corpus" / corpus_id
    (root / "project").mkdir(parents=True, exist_ok=True)
    (root / "project" / "REQUIREMENT.md").write_text(data["requirement_md"], encoding="utf-8")
    (root / "project" / data["module_name"]).write_text(data["module_code"], encoding="utf-8")
    (root / "ground_truth.json").write_text(
        json.dumps(data["ground_truth"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK {corpus_id}: {data['module_name']}，植入 {len(data['ground_truth']['planted_defects'])} 个缺陷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
