"""C5 场景轨三语言真机验收（2026-11-13，零 LLM）：JS supertest / Java HttpClient /
PHP curl 顺序场景（POST 创建→按 id 读回一致）三 demo 容器真跑。

数据源全确定性（零 LLM）：sequence_scenarios 端点配对（callgraph 白盒轨真实函数）
+ example_from_schema happy path（contract_gen 真实函数）——渲染层 = render_sequence_
js/java/php（与 Python 金标准 render_sequence_module 同语义）。

防假绿铁律（F 卡 §6 坑预判 3）：用例数>0 且断言真执行——jest "N passed" /
maven "Tests run: N, Failures: 0" / phpunit "OK (N tests, M assertions)" 原生计行，
场景数 0 或执行器 0 用例一律 FAIL。

用法：python scripts/demo_c5_sequence.py [--lang js|java|php]（缺省三语言全跑）
产物：data/reports/c5_sequence/<lang>/（渲染文件副本 + 执行输出尾部 + 结论 JSON）
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.sandbox.runner import SandboxRunner  # noqa: E402
from test_agent.skills.callgraph import sequence_scenarios  # noqa: E402
from test_agent.skills.contract_gen import example_from_schema  # noqa: E402
from test_agent.skills.contract_multilang import (render_sequence_java,  # noqa: E402
                                                  render_sequence_js,
                                                  render_sequence_php)
from test_agent.graph.l2 import (_start_service_java, _start_service_js,  # noqa: E402
                                 _start_service_php)

OUT = ROOT / "data" / "reports" / "c5_sequence"
DEMO = ROOT / "data" / "demo"

_DEMO_COPY_EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "node_modules", "coverage", "vendor", "target", ".git")

# 每语言端点夹具（确定性，如实反映 demo 行为：js POST 201 / java·php POST 200）
_POST_SCHEMA = {"type": "object", "required": ["title"],
                "properties": {"title": {"type": "string", "example": "scene-book"}}}


def _endpoints(post_code: int) -> list[dict]:
    return [
        {"method": "POST", "path": "/books", "params": [],
         "request_schema": _POST_SCHEMA, "response_schema": None,
         "success_code": post_code},
        {"method": "GET", "path": "/books/{id}", "params": [], "request_schema": None,
         "response_schema": None, "success_code": 200},
        {"method": "GET", "path": "/books", "params": [], "request_schema": None,
         "response_schema": None, "success_code": 200},
    ]


def _counts_js(out: str) -> tuple[int, int, str]:
    ok = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    bad = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    bad += int(m.group(1)) if (m := re.search(r"(\d+) errored", out)) else 0
    return ok, bad, "jest 计行"


def _counts_java(out: str) -> tuple[int, int, str]:
    # 只取最后一条 Tests run（maven 总计行在最后）——类行+总计行会重复计数（首跑实锤 1 用例计成 2）
    runs = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", out)
    if not runs:
        return 0, 0, "maven 无 Tests run 计行"
    total, fails, errs = (int(x) for x in runs[-1])
    return total - fails - errs, fails + errs, "maven surefire 总计行"


def _counts_php(out: str) -> tuple[int, int, str]:
    ok = int(m.group(1)) if (m := re.search(r"OK \((\d+) tests?", out)) else 0
    bad = int(m.group(1)) if (m := re.search(r"Tests: (\d+), Assertions: \d+(?:,\s*)?(\d+)", out)) else 0
    return ok, bad, "phpunit 计行"


def _run_lang(lang: str, runner: SandboxRunner) -> dict:
    """单语言真机：tmp 工作区 → 渲染 → 容器装依赖 → 起服务 → 原生测试 → 原生计行。"""
    out_dir = OUT / lang
    out_dir.mkdir(parents=True, exist_ok=True)
    demo = {"js": DEMO / "js-api-book", "java": DEMO / "java-api-book",
            "php": DEMO / "php-api-book"}[lang]
    image = {"js": config.IMAGE_JS, "java": config.IMAGE_JAVA,
             "php": config.IMAGE_PHP}[lang]
    cname = f"mta-c5-demo-{lang}"

    ws = ROOT / "data" / "reports" / "c5_sequence" / f"_ws_{lang}"
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(demo, ws, ignore=_DEMO_COPY_EXCLUDE)

    # 1) 确定性场景数据（sequence_scenarios 真实配对 + example_from_schema 真 payload）
    post_code = {"js": 201, "java": 200, "php": 200}[lang]
    eps = _endpoints(post_code)
    scs = sequence_scenarios(eps)
    happy = {"POST /books": example_from_schema(_POST_SCHEMA)}
    if not scs:
        return {"lang": lang, "pass": False, "why": "sequence_scenarios 配对 0 场景"}
    # 2) 渲染 + 落工作区
    if lang == "js":
        code, cases = render_sequence_js(scs, happy, "http://127.0.0.1:8080")
        (ws / "sequence.test.js").write_text(code, encoding="utf-8")
    elif lang == "java":
        code, cases = render_sequence_java(scs, happy, "http://127.0.0.1:8000")
        (ws / "src/test/java/com/demo/SequenceScenariosTest.java").write_text(
            code, encoding="utf-8")
    else:
        code, cases = render_sequence_php(scs, happy, "http://127.0.0.1:8000")
        (ws / "SequenceScenariosTest.php").write_text(code, encoding="utf-8")
    shutil.copy2(ws / {"js": "sequence.test.js",
                       "java": "src/test/java/com/demo/SequenceScenariosTest.java",
                       "php": "SequenceScenariosTest.php"}[lang], out_dir)

    t0 = time.time()
    try:
        container = runner.ensure_container(image, cname, fresh=True)
        runner.copy_dir_into(container, ws)
        # 3) 依赖（按语言；java 先预热编译——首次 mvn 下载依赖远超服务就绪 30s 轮询窗）
        if lang == "js":
            r = runner.exec_in(container, ["npm", "install", "--no-audit", "--no-fund",
                                           "--loglevel=error"], timeout_s=600)
            if r.exit_code != 0:
                return {"lang": lang, "pass": False, "why": f"npm install 失败：{r.stderr[-200:]}"}
        elif lang == "php":
            r = runner.exec_in(container, ["composer", "install", "--no-interaction",
                                           "--no-progress", "--quiet"], timeout_s=600)
            if r.exit_code != 0:
                return {"lang": lang, "pass": False,
                        "why": f"composer install 失败：{(r.stderr or r.stdout)[-200:]}"}
        else:
            # 预热用 package 而非 compile：spring-boot repackage 绑定 package 阶段，
            # 只有它能把 spring-boot-maven-plugin 全树拉热——compile 预热后
            # spring-boot:run 仍会静默下载插件数分钟，30s 就绪窗口必超时（真机实锤）。
            # 冷下载大树偶发单包传输失败（DependencyResolutionException）→重试一次，
            # maven 幂等续传只补缺的包
            r = runner.exec_in(container, ["mvn", "-q", "-DskipTests", "package"],
                               timeout_s=1800)
            if r.exit_code != 0:
                r = runner.exec_in(container, ["mvn", "-q", "-DskipTests", "package"],
                                   timeout_s=1800)
            if r.exit_code != 0:
                return {"lang": lang, "pass": False,
                        "why": f"mvn package 预热失败（含重试）：{(r.stderr or r.stdout)[-300:]}"}
        # 4) 起服务（复用 C5 L3 prepare 同款模块级启动器——同一代码路径）
        port = {"js": 8080, "java": 8000, "php": 8000}[lang]
        if lang == "js":
            ok, why = _start_service_js(runner, container, "app.js", port)
        elif lang == "java":
            ok, why = _start_service_java(runner, container, "com.demo.DemoApplication", port)
        else:
            ok, why = _start_service_php(runner, container, port)
        if not ok:
            return {"lang": lang, "pass": False, "why": f"服务未就绪：{why}"}
        # 5) 原生测试 + 原生计行（防假绿：断言真执行的执行器证据）
        # 计行解析统一吃 stdout+stderr（jest 计行在 stderr——首跑实锤；maven/phpunit 在 stdout）
        if lang == "js":
            r = runner.exec_in(container, ["npx", "jest", "sequence", "--colors=false"],
                               timeout_s=300)
            ok_n, bad_n, how = _counts_js((r.stdout or "") + "\n" + (r.stderr or ""))
        elif lang == "java":
            r = runner.exec_in(container, ["mvn", "test", "-Dtest=SequenceScenariosTest"],
                               timeout_s=1200)
            ok_n, bad_n, how = _counts_java((r.stdout or "") + "\n" + (r.stderr or ""))
        else:
            r = runner.exec_in(container, ["vendor/bin/phpunit", "SequenceScenariosTest.php"],
                               timeout_s=300)
            ok_n, bad_n, how = _counts_php((r.stdout or "") + "\n" + (r.stderr or ""))
        verdict = {"lang": lang, "scenarios": len(scs), "cases": len(cases),
                   "passed": ok_n, "failed": bad_n, "counter": how,
                   "exit": r.exit_code, "seconds": round(time.time() - t0, 1),
                   "pass": ok_n > 0 and bad_n == 0 and r.exit_code == 0,
                   "evidence": (r.stdout or "")[-1200:]}
        (out_dir / "run_output.txt").write_text(r.stdout or "", encoding="utf-8")
        (out_dir / "run_stderr.txt").write_text(r.stderr or "", encoding="utf-8")
        (out_dir / "verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
        return verdict
    finally:
        try:
            runner.cleanup(cname)
        except Exception as e:  # noqa: BLE001
            print(f"[cleanup] {cname}: {e}")
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["js", "java", "php"])
    args = ap.parse_args()
    langs = [args.lang] if args.lang else ["js", "java", "php"]
    runner = SandboxRunner()
    results = []
    for lang in langs:
        print(f"[c5-sequence] === {lang} ===", flush=True)
        v = _run_lang(lang, runner)
        results.append(v)
        mark = "PASS" if v.get("pass") else "FAIL"
        print(f"[c5-sequence] {lang}: {mark} — 场景 {v.get('scenarios')} 条/"
              f"用例 {v.get('cases')} 条/通过 {v.get('passed')}/失败 {v.get('failed')}"
              f"（{v.get('counter')}） {v.get('seconds', '?')}s", flush=True)
        if not v.get("pass"):
            print(f"  why={v.get('why') or v.get('evidence', '')[-600:]}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    all_pass = all(v.get("pass") for v in results)
    print(f"[c5-sequence] 总结：{'三语言全 PASS' if all_pass else '存在 FAIL'}"
          f"（防假绿口径：用例数>0 且原生计行 0 失败）")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
