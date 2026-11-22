# -*- coding: utf-8 -*-
"""修复验证真机验收·零 LLM 格 G1-G6（2026-11-20 设计文档 docs/修复验证真机验收设计-20261120.md §1）。

每格预注册判据见设计文档；结果落 run_zero_llm_results.json + 控制台判定表。
G1/G2/G6 对 js-calc 真实仓库做可逆变异（try/finally 保证清理）。
G4/G5 走真机 server（127.0.0.1:8000，临时注册账号取 token）。
零 LLM、零付费调用。
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

JSC = ROOT / "data" / "demo" / "js-calc"
OUT = Path(__file__).resolve().parent / "run_zero_llm_results.json"
API = "http://127.0.0.1:8000"

results = []


def record(gid, name, passed, detail):
    results.append({"gid": gid, "name": name, "pass": bool(passed), "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {gid} {name}: {detail}", flush=True)


# ---------- G1 OpenAPI 死分支复活 ----------

def g1():
    try:
        spec = {"openapi": "3.0.0", "info": {"title": "verify"}, "paths": {
            "/pets": {"get": {"summary": "list pets"}},
            "/owners": {"post": {"summary": "create owner"}}}}
        (JSC / "openapi.json").write_text(json.dumps(spec), encoding="utf-8")
        from test_agent.skills.project_index import build_index
        idx = build_index(JSC, ["javascript"])
        eps = [e for e in idx.get("endpoints", []) if e.get("source") == "openapi.json"]
        paths = sorted(e.get("path") for e in eps)
        ok = "/pets" in paths and "/owners" in paths
        # 反向断言：spec 不污染模块标签（模块只有 js 目录语义）
        record("G1", "OpenAPI 死分支复活", ok,
               f"spec 端点注册 {paths}；modules 键={sorted(idx.get('modules', {}).keys())[:6]}")
    finally:
        (JSC / "openapi.json").unlink(missing_ok=True)


# ---------- G2 缓存指纹 ----------

def g2():
    from test_agent.skills.project_index import INDEX_ALGO, ensure_index
    probe = JSC / "zz_fixverify_probe.py"
    try:
        idx1 = ensure_index(99001, JSC, ["javascript"], force=True)
        idx2 = ensure_index(99001, JSC, ["javascript"])
        reuse = (idx2.get("src_fingerprint") == idx1.get("src_fingerprint")
                 and idx2.get("index_algo") == INDEX_ALGO)
        probe.write_text("def fixverify_probe(): pass\n", encoding="utf-8")
        time.sleep(0.05)   # mtime_ns 粒度护栏
        idx3 = ensure_index(99001, JSC, ["javascript"])
        rebuilt = "zz_fixverify_probe.py" in json.dumps(idx3.get("modules", {}))
        record("G2", "缓存指纹校验", reuse and rebuilt,
               f"复用={reuse}（fp={idx1.get('src_fingerprint', '')[:8]}）；"
               f"新增文件触发重建={rebuilt}")
    finally:
        probe.unlink(missing_ok=True)
        ensure_index(99001, JSC, ["javascript"], force=True)   # 还原索引


# ---------- G3 需求侧词边界 ----------

def g3():
    from test_agent.skills.project_index import build_index, match_requirement
    ws = Path(tempfile.mkdtemp(prefix="fixverify_g3_"))
    try:
        (ws / "log").mkdir(); (ws / "login").mkdir(); (ws / "order").mkdir()
        (ws / "log" / "audit.py").write_text("def audit(): pass\n", encoding="utf-8")
        (ws / "login" / "views.py").write_text("def login_view(): pass\n", encoding="utf-8")
        (ws / "order" / "core.py").write_text("def o(): pass\n", encoding="utf-8")
        idx = build_index(ws, ["python"])
        h1 = match_requirement(idx, "test the login page")
        h2 = match_requirement(idx, "test the log module")
        ok = (h1["kind"] != "module" or h1["module"] != "log") and \
             (h2["kind"] == "module" and h2["module"] == "log")
        record("G3", "需求侧词边界", ok,
               f"login 需求命中={h1.get('module', h1['kind'])}（应非 log）；"
               f"log 需求命中={h2.get('module', h2['kind'])}（应=log）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------- G4/G5 API 格（真机 server） ----------

def _token():
    import httpx
    cred = {"username": "fixverify", "password": "Fv#20261120"}
    r = httpx.post(f"{API}/api/register", json=cred, timeout=10)
    r = httpx.post(f"{API}/api/login", json=cred, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def g4():
    import httpx
    tk = _token()
    r = httpx.post(f"{API}/api/tasks", json={"repo_url": "\\\\evil\\share\\repo",
                                             "requirement": "unc test"},
                   headers={"Authorization": f"Bearer {tk}"}, timeout=10)
    ok = r.status_code == 400 and "UNC" in (r.json().get("detail") or "")
    record("G4", "UNC 路径 400", ok, f"HTTP {r.status_code} detail={r.json().get('detail', '')[:80]}")


def g5():
    import httpx
    tk = _token()
    r = httpx.post(f"{API}/api/llm-profile/test",
                   json={"provider": "custom", "base_url": "https://evil.example.com/v1",
                         "model": "x", "key_env": "DEEPSEEK_API_KEY"},
                   headers={"Authorization": f"Bearer {tk}"}, timeout=10)
    body = r.json()
    ok = body.get("ok") is False and "白名单" in (body.get("error") or "")
    record("G5", "连通性白名单", ok,
           f"ok={body.get('ok')} error={str(body.get('error', ''))[:80]}（拒绝在发请求前=零外发）")


# ---------- G6 真机索引链路（js-calc + 可逆嵌套注入） ----------
# 【判据修订 2026-11-20】初版判据"测试计算器命中模块"判据错：js-calc 单目录仓全归
# _root，按设计（E2E-1 防白锁）单模块仓不参与路由=设计使然非缺陷。修订为可逆注入
# 嵌套布局（src/user/）——直接验证 A3 修复核心场景：模块名 user 但真实路径 src/user。

def g6():
    from test_agent.skills.project_index import (build_index, match_requirement,
                                                 scope_prefixes_for)
    injected = JSC / "src" / "user" / "profile.js"
    try:
        injected.parent.mkdir(parents=True, exist_ok=True)
        injected.write_text("export function getUserProfile(id) { return id; }\n",
                            encoding="utf-8")
        idx = build_index(JSC, ["javascript"])
        hit = match_requirement(idx, "测试 user 模块的用户资料功能")
        if hit.get("kind") != "module" or hit.get("module") != "user":
            record("G6", "真机索引链路（嵌套布局注入）", False,
                   f"需求未命中 user 模块：{hit}")
            return
        prefixes = scope_prefixes_for(idx, [hit["module"]])
        ok = prefixes == ["src/user"], \
            f"scope 必须是真实路径 src/user（修复前拼模块名=user/ 永远锁空）"
        record("G6", "真机索引链路（嵌套布局注入）", bool(ok[0]), ok[1] + f"；实际={prefixes}")
    finally:
        # 事故修复（2026-11-21 欠账清偿）：原 rmtree(JSC/"src") 会把宿主 calc.js
        # 一起删（G7/G8 前两跑主语言 unknown 的事故根源）——改为精确清理：
        # 只删注入文件 + 空目录才删 + calc.js 完好性断言（防复发）
        injected.unlink(missing_ok=True)
        user_dir = JSC / "src" / "user"
        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            pass
        calc = JSC / "src" / "calc.js"
        calc_ok = calc.exists() and "clamp" in calc.read_text(encoding="utf-8",
                                                              errors="replace")
        record("G6", "可逆清理不伤宿主（calc.js 完好性断言）", calc_ok,
               "calc.js 完好且含 clamp" if calc_ok
               else "calc.js 被清理破坏（事故复发，立即从 git 恢复）")
        if not calc_ok:
            import subprocess as _sp
            _sp.run(["git", "checkout", "--", "src/calc.js"], cwd=str(JSC),
                    capture_output=True)
        from test_agent.skills.project_index import ensure_index
        ensure_index(99002, JSC, ["javascript"], force=True)   # 还原索引


if __name__ == "__main__":
    for fn in (g1, g2, g3, g6, g4, g5):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 —— 格失败留痕继续下一格
            record("ERR", fn.__name__, False, f"{type(e).__name__}: {e}")
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    n_pass = sum(1 for r in results if r["pass"])
    print(f"\n判定: {n_pass}/{len(results)} PASS → {OUT}", flush=True)
    sys.exit(0 if n_pass == len(results) else 1)
