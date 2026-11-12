"""本地 Ollama 速度基准：真实 gen_tests prompt × num_gpu/ctx 组合扫描。

直接打 /api/chat（原生端点），读 eval_count / eval_duration / prompt_eval_count，
而不是靠字符数除时间——字符→token 的换算在中文/JSON 混排下误差能到 2 倍。

用法：
  python scripts/bench_local_llm.py                      # 扫默认矩阵
  python scripts/bench_local_llm.py --num-gpu 99         # 只测指定档
  python scripts/bench_local_llm.py --model qwen3.5:4b
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.skills.prompt_library import SYSTEM_PROMPT, build_user_prompt

API = "http://localhost:11434/api"
CALC_SRC = '''def div(a, b):
    """整数除法；除数为 0 抛 ValueError。"""
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("arguments must be int")
    if b == 0:
        raise ValueError("division by zero")
    return a // b
'''
REQUIREMENT = "验证 div 函数：正常整除、除零保护、非整数类型拒绝、边界值（最小整数附近）。"


def _post(path, payload, timeout=900):
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_ps(model):
    try:
        for m in _post_ps():
            if m["name"].startswith(model.split(":")[0]):
                return m
    except Exception:
        pass
    return None


def _post_ps():
    with urllib.request.urlopen(f"{API}/ps", timeout=10) as r:
        return json.loads(r.read()).get("models", [])


def run(model, num_gpu, num_ctx, num_predict, prompt, sys_prompt, think):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": 0.2,
            "num_gpu": num_gpu,
        },
    }
    if think is not None:
        payload["think"] = think
    t0 = time.time()
    r = _post("chat", payload)
    wall = time.time() - t0

    ev = r.get("eval_count", 0)
    ev_ns = r.get("eval_duration", 1) or 1
    pe = r.get("prompt_eval_count", 0)
    pe_ns = r.get("prompt_eval_duration", 1) or 1
    ps = get_ps(model)
    vram = (ps or {}).get("size_vram", 0)
    total = (ps or {}).get("size", 0)
    msg = r.get("message", {})
    content = msg.get("content", "") or ""
    thinking = msg.get("thinking", "") or ""

    # 质量验收走生产同款解析器（_parse_cases），而不是看输出像不像 JSON
    from test_agent.skills.gen_tests import _parse_cases

    data = _parse_cases(content)
    n_cases = len(data["cases"]) if data else 0
    methods = sorted({c.get("method", "?") for c in data["cases"]}) if data else []

    return {
        "model": model,
        "num_gpu": num_gpu,
        "num_ctx": num_ctx,
        "wall_s": wall,
        "prompt_tok": pe,
        "prefill_tps": pe / (pe_ns / 1e9) if pe else 0,
        "gen_tok": ev,
        "gen_tps": ev / (ev_ns / 1e9) if ev else 0,
        "vram_mb": vram / 1024 / 1024,
        "total_mb": total / 1024 / 1024,
        "content_chars": len(content),
        "thinking_chars": len(thinking),
        "parse_ok": data is not None,
        "n_cases": n_cases,
        "methods": methods,
        "preview": content[:120].replace("\n", " "),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-4b-inst-test")
    ap.add_argument("--num-gpu", type=int, default=None)
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--num-predict", type=int, default=3000)
    ap.add_argument("--think", default=None, choices=["true", "false"])
    ap.add_argument("--matrix", action="store_true", help="扫 num_gpu 矩阵")
    args = ap.parse_args()

    prompt = build_user_prompt([("calc.py", CALC_SRC)], REQUIREMENT, include_methods=True)
    sys_prompt = SYSTEM_PROMPT
    if "qwen3" in args.model.lower() and "instruct" not in args.model.lower():
        sys_prompt += " /no_think"
    think = None if args.think is None else (args.think == "true")

    print(f"[bench] model={args.model} prompt_chars={len(prompt)} num_ctx={args.num_ctx}")
    print(f"{'num_gpu':>8} {'gen_tok':>8} {'tok/s':>7} {'prefill/s':>10} "
          f"{'vram_MB':>8} {'parse':>6} {'cases':>6} {'wall_s':>7}  methods")

    matrix = [args.num_gpu] if args.num_gpu is not None else ([0, 10, 20, 28, 40, 99] if args.matrix else [None])
    results = []
    for ng in matrix:
        try:
            r = run(args.model, ng, args.num_ctx, args.num_predict, prompt, sys_prompt, think)
        except Exception as e:
            print(f"{str(ng):>8}  FAIL {type(e).__name__}: {str(e)[:70]}")
            continue
        results.append(r)
        tag = "auto" if ng is None else str(ng)
        print(f"{tag:>8} {r['gen_tok']:>8} {r['gen_tps']:>7.2f} {r['prefill_tps']:>10.1f} "
              f"{r['vram_mb']:>8.0f} {'OK' if r['parse_ok'] else 'FAIL':>6} {r['n_cases']:>6} "
              f"{r['wall_s']:>7.1f}  {'/'.join(r['methods'])[:40]}")

    if results:
        best = max(results, key=lambda x: x["gen_tps"])
        print(f"\n[best-speed] num_gpu={best['num_gpu']} gen={best['gen_tps']:.2f} tok/s "
              f"vram={best['vram_mb']:.0f}MB parse={'OK' if best['parse_ok'] else 'FAIL'} "
              f"cases={best['n_cases']}")
        out = Path("docs/bench-local-llm-latest.json")
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {out}")


if __name__ == "__main__":
    main()
