# -*- coding: utf-8 -*-
"""R1 通道烟测（E10 Wave 1 前置，~2k token 成本）。

验证：zcode 赠款通道能否调用 deepseek-reasoner（可能 404/无权限）。
成功输出：回复 + reasoning token 计数形态（决定 E10 Arm 2 记账口径）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from openai import OpenAI

key = os.environ.get("DEEPSEEK_API_KEY")
if not key:
    print("NO_KEY：DEEPSEEK_API_KEY 未配置")
    sys.exit(2)

client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
try:
    r = client.chat.completions.create(
        model="deepseek-reasoner",
        messages=[{"role": "user", "content": "1+1=?只回答数字。"}],
        max_tokens=200,
    )
    msg = r.choices[0].message
    usage = r.usage
    print("MODEL:", r.model)
    print("CONTENT:", (msg.content or "")[:80])
    reasoning = getattr(msg, "reasoning_content", None)
    print("REASONING_FIELD:", ("有，长度 " + str(len(reasoning or ""))) if reasoning else "无")
    print("USAGE: prompt =", usage.prompt_tokens,
          "completion =", usage.completion_tokens,
          "completion_details =", getattr(usage, "completion_tokens_details", None))
    print("R1_CHANNEL_OK")
except Exception as e:
    print(f"R1_CHANNEL_FAIL: {type(e).__name__}: {str(e)[:300]}")
    sys.exit(1)
