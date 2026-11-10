"""分层容量探针（任务卡-C 验收口径"容器层/LLM 层两条线分述"，2026-11-10）。

端到端任务（run_concurrency_probe.py）的墙钟被本地模型方差主导，分不清瓶颈在哪层；
本脚本把两层拆开各自直测，结论才能分线陈述：

  容器层：python scripts/run_capacity_layers.py --layer container --n 5
    N 个真实沙箱容器同时走「建容器→快照→冷执行→3 轮热执行→回收」，
    负载=data/demo/场景A（真实 pytest），无 LLM 参与。
  LLM 层：python scripts/run_capacity_layers.py --layer llm --n 5 [--profile local|deepseek]
    N 路并发请求同一端点，固定提示词（corpus-003 需求+源码≈2k token）、
    输出上限 256 token；测单路延迟、单路 tok/s、聚合吞吐。

产物：data/reports/concurrency/layer_<layer>_N<n>_<profile>_<ts>.json
诚实边界：单机；容器负载为微型套件（测的是"编排与隔离开销的并发伸缩"，
不是重负载吞吐）；LLM 层输出截到 256 token（测并发行为，不测长输出质量）。
"""
import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

CONC_DIR = ROOT / "data" / "reports" / "concurrency"
SCENARIO = ROOT / "data" / "demo" / "场景A"
CORPUS_PROJ = ROOT / "data" / "corpus" / "corpus-003" / "project"
PYTEST_CMD = ["pytest", "-q", "--tb=short", "-rfE"]
WARM_ROUNDS = 3
LLM_MAX_TOKENS = 256


def _sampler(samples: list, stop: threading.Event) -> None:
    psutil.cpu_percent(interval=None)
    i = 0
    while not stop.is_set():
        row = {"t": round(time.monotonic(), 1),
               "cpu_percent": psutil.cpu_percent(interval=1),
               "mem_percent": psutil.virtual_memory().percent}
        if i % 2 == 0:
            try:
                r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                   capture_output=True, text=True, timeout=20)
                row["cap_containers"] = sum(1 for ln in r.stdout.splitlines()
                                            if ln.startswith("mta-cap-"))
            except Exception as e:                # noqa: BLE001
                row["docker_err"] = type(e).__name__
        samples.append(row)
        i += 1
        stop.wait(2)


# ---------------- 容器层 ----------------

def _container_one(idx: int, barrier: threading.Barrier) -> dict:
    from test_agent import config
    from test_agent.sandbox.runner import SandboxRunner
    runner = SandboxRunner()
    name = f"mta-cap-{idx}"
    runner.cleanup(name)
    barrier.wait()
    seg = {}
    t0 = time.perf_counter()
    try:
        container = runner.ensure_container(config.IMAGE_PY, name, fresh=True)
        seg["create_s"] = time.perf_counter() - t0
        t1 = time.perf_counter()
        runner.copy_dir_into(container, SCENARIO)
        seg["snapshot_s"] = time.perf_counter() - t1
        t2 = time.perf_counter()
        r = runner.exec_in(container, PYTEST_CMD)
        seg["cold_exec_s"] = time.perf_counter() - t2
        seg["cold_e2e_s"] = time.perf_counter() - t0
        out = (r.stdout or "") + (r.stderr or "")
        seg["ran_ok"] = ("passed" in out) or ("failed" in out)
        warm = []
        for _ in range(WARM_ROUNDS):
            t = time.perf_counter()
            runner.exec_in(container, PYTEST_CMD)
            warm.append(time.perf_counter() - t)
        seg["warm_exec_mean_s"] = statistics.mean(warm)
        seg["warm_exec_all_s"] = [round(w, 3) for w in warm]
    except Exception as e:                        # noqa: BLE001 —— 失败如实留痕
        seg["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        try:
            runner.cleanup(name)
        except Exception:                         # noqa: BLE001
            pass
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in seg.items()}


def run_container_layer(n: int) -> dict:
    samples: list = []
    stop = threading.Event()
    th = threading.Thread(target=_sampler, args=(samples, stop), daemon=True)
    barrier = threading.Barrier(n)
    t0 = time.monotonic()
    th.start()
    with ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(lambda i: _container_one(i, barrier), range(n)))
    wall = time.monotonic() - t0
    stop.set()
    th.join(timeout=8)
    ok = [r for r in rows if "error" not in r]
    def _mean(key):
        vals = [r[key] for r in ok if key in r]
        return round(statistics.mean(vals), 3) if vals else None
    return {"layer": "container", "n": n, "batch_wall_s": round(wall, 2),
            "ok": len(ok), "per_container": rows,
            "mean": {k: _mean(k) for k in ("create_s", "snapshot_s", "cold_exec_s",
                                           "cold_e2e_s", "warm_exec_mean_s")},
            "peak_containers": max((s.get("cap_containers", 0) for s in samples),
                                   default=0),
            "cpu_peak": max((s["cpu_percent"] for s in samples), default=None),
            "cpu_mean": (round(statistics.mean(s["cpu_percent"] for s in samples), 1)
                         if samples else None),
            "mem_peak": max((s["mem_percent"] for s in samples), default=None),
            "samples": samples}


# ---------------- LLM 层 ----------------

def _llm_prompt() -> str:
    req = (CORPUS_PROJ / "REQUIREMENT.md").read_text(encoding="utf-8")
    srcs = "".join((CORPUS_PROJ / n).read_text(encoding="utf-8")
                   for n in ("app.py", "booking.py", "store.py"))
    return ("你是测试分析员。阅读需求与源码，输出 JSON：{\"conditions\": [...]}，"
            "列出测试条件。\n\n需求：\n" + req + "\n\n源码：\n" + srcs)


def _llm_one(idx: int, barrier: threading.Barrier, base_url: str, model: str,
             api_key: str, prompt: str) -> dict:
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key or "x", timeout=900,
                    max_retries=0)
    barrier.wait()
    t0 = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=LLM_MAX_TOKENS, temperature=0)
        dt = time.perf_counter() - t0
        u = resp.usage
        ct = getattr(u, "completion_tokens", 0) or 0
        return {"idx": idx, "latency_s": round(dt, 2),
                "prompt_tokens": getattr(u, "prompt_tokens", 0),
                "completion_tokens": ct,
                "tok_per_s": round(ct / dt, 2) if dt else None}
    except Exception as e:                        # noqa: BLE001 —— 429/超时如实留痕
        return {"idx": idx, "latency_s": round(time.perf_counter() - t0, 2),
                "error": f"{type(e).__name__}: {e}"[:200]}


def run_llm_layer(n: int, profile: str) -> dict:
    from test_agent.skills import llm_profile
    preset = llm_profile.PRESETS[profile]
    base_url, model = preset["base_url"], preset["model"]
    api_key = llm_profile.resolve_key(preset) or ""
    prompt = _llm_prompt()
    barrier = threading.Barrier(n)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(lambda i: _llm_one(i, barrier, base_url, model,
                                              api_key, prompt), range(n)))
    wall = time.monotonic() - t0
    ok = [r for r in rows if "error" not in r]
    total_ct = sum(r["completion_tokens"] for r in ok)
    return {"layer": "llm", "n": n, "profile": profile, "model": model,
            "max_tokens": LLM_MAX_TOKENS, "batch_wall_s": round(wall, 2),
            "ok": len(ok), "errors": [r["error"] for r in rows if "error" in r],
            "per_request": rows,
            "latency_mean_s": (round(statistics.mean(r["latency_s"] for r in ok), 2)
                               if ok else None),
            "latency_max_s": max((r["latency_s"] for r in ok), default=None),
            "tok_per_s_single_mean": (round(statistics.mean(
                r["tok_per_s"] for r in ok if r.get("tok_per_s")), 2) if ok else None),
            "throughput_tok_per_s": round(total_ct / wall, 2) if wall else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=("container", "llm"), required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--profile", choices=("local", "deepseek"), default="local")
    a = ap.parse_args()
    rep = (run_container_layer(a.n) if a.layer == "container"
           else run_llm_layer(a.n, a.profile))
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    tag = a.profile if a.layer == "llm" else "docker"
    out = CONC_DIR / f"layer_{a.layer}_N{a.n}_{tag}_{ts}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items()
                      if k not in ("samples", "per_container", "per_request")},
                     ensure_ascii=False))
    print(f"已落档 {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
