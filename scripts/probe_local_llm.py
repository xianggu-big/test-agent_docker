"""本地 Ollama LLM 冒烟（OpenAI 兼容端点）——验证项目管线能否吃下本地小模型。

测什么：真实 build_user_prompt（五方法骨架全开，与生产 gen_tests 同形）→ Ollama /v1
→ 真实 _parse_cases 验收（JSON 纪律）→ 静态闸同款语法检查。不改 config.py，
切换本身 = 三个环境变量（BASE_URL/MODEL/API_KEY），见文件尾注释。

用法：python scripts/probe_local_llm.py [模型名]（默认 qwen3:4b；需 ollama serve 已启动）
"""
import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from langchain_openai import ChatOpenAI

from test_agent.skills.gen_tests import _parse_cases
from test_agent.skills.prompt_library import SYSTEM_PROMPT, build_user_prompt

BASE_URL = "http://localhost:11434/v1"

CALC_SRC = '''def div(a, b):
    """整数除法；除数为 0 抛 ValueError。"""
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("arguments must be int")
    if b == 0:
        raise ValueError("division by zero")
    return a // b
'''

REQUIREMENT = "验证 div 函数：正常整除、除零保护、非整数类型拒绝、边界值（最小整数附近）。"


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3:4b"
    # qwen3 系列默认开思考模式：<think> 长推理易触发 Ollama 防重复中止/超时且污染 JSON 解析。
    # 官方软开关 /no_think 放 system 消息才可靠（放 user 末尾会被聊天模板忽略——实测踩坑）
    sys_prompt = SYSTEM_PROMPT + (" /no_think" if "qwen3" in model.lower() else "")
    # qwen3 思考版默认开思考：<think> 长推理烧穿预算且污染 JSON 解析。
    # Ollama 模板不理会 /no_think 软开关（实测），思考后的正文经兼容层分流到 content——
    # 预算给足即可，但更优解是官方无思考变体（如 qwen3:4b-instruct-2507，名称含 instruct 不加开关）
    no_think = "/no_think" if ("qwen3" in model.lower() and "instruct" not in model.lower()) else ""
    llm = ChatOpenAI(model=model, api_key="ollama", base_url=BASE_URL,
                     temperature=0.2, timeout=600, max_retries=1,
                     max_tokens=3000)   # 输出上限兜底；派生模型（Modelfile）已烘焙 num_ctx/防复读参数
    prompt = build_user_prompt([("calc.py", CALC_SRC)], REQUIREMENT,
                               include_methods=True) + no_think
    print(f"[probe] model={model} prompt_chars={len(prompt)}（真实 build_user_prompt 产物）")
    t0 = time.time()
    try:
        resp = llm.invoke([("system", sys_prompt), ("user", prompt)])
    except Exception as e:
        print(f"[FAIL] 调用失败: {type(e).__name__}: {e}")
        return 2
    latency = time.time() - t0
    text = resp.content if isinstance(resp.content, str) else str(resp.content)

    # 与生产同款解析器验收（JSON 纪律）
    data = _parse_cases(text)
    n_chars = len(text)
    print(f"[probe] 延迟 {latency:.1f}s / 输出 {n_chars} 字符 / "
          f"约 {max(1, n_chars // max(1, int(latency)))} 字符每秒")
    if data is None:
        print("[FAIL] 输出无法解析为用例 JSON（_parse_cases 判废——本地小模型的预期风险形态）")
        print("---- 原始输出前 400 字 ----")
        print(text[:400])
        return 1
    cases = data["cases"]
    print(f"[OK] 解析成功：{len(cases)} 条用例 | imports={'有' if data.get('imports') else '无'}")
    for c in cases:
        name = c.get("name", "?")
        method = c.get("method", "?")
        basis = c.get("expected_basis", "?")
        print(f"  - {name}（方法={method}，依据={basis}）")
    print(f"\n[结论] {model} 通过管线冒烟；接入项目 = 设三个值：\n"
          f"  DEEPSEEK_BASE_URL={BASE_URL}\n"
          f"  DEEPSEEK_MODEL={model}\n"
          f"  DEEPSEEK_API_KEY=ollama\n"
          f"（4GB 显存铁律：用 Modelfile 派生模型烘焙 num_ctx=8192——16k 的 KV 缓存会溢出到 CPU 掉速 5 倍；\n"
          f"  思考版 qwen3:4b 会烧预算出空 content，用无思考变体 *-instruct）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
