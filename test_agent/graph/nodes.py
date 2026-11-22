"""LangGraph 节点：clone → analyze → provision → generate → run → evaluate → report。

约定：
- 节点进入时先落 test_tasks.status（界面进度条与状态机一一对应）
- 关键动作全部经 SkillRegistry.invoke（审计台账 + 确认门）
- 容器里只跑测试；归因判读（Evaluator）在宿主侧 —— 执行与判定分离
"""
import io
import json
import logging
import os
import re
import fnmatch
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..sandbox.runner import SandboxRunner
from ..skills.builtin import build_skills, probe_deps_manifest
from ..skills.defect_handoff import auto_export_handoff
from ..skills.evaluate import build_eval_skills
from ..skills.llm_usage import log_llm_call
from ..skills.repair_strategy import make_deepseek_fn, run_repair_strategy
from ..skills.prompt_library import PLAN_SYSTEM, build_plan_prompt
from ..skills.registry import SkillRegistry
from ..store.db import get_conn
from . import repair
from .state import TaskState

log = logging.getLogger(__name__)

_EXT_LANG = {".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
             ".jsx": "javascript", ".tsx": "typescript", ".go": "go", ".php": "php", ".c": "c", ".cpp": "cpp"}

_GH_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def build_run_cmd(round_no: int = 1, gen_files: list[str] | None = None) -> list[str]:
    """L1 主图 pytest 命令（Loop-8 抽出供单测；Loop-9 参数化）。

    --continue-on-collection-errors（Loop-8/D10 后继，三角色实验 30 轮实锤）：tenacity 案例中
    仓库自带 tests/ 缺依赖（tornado）→ 收集中断『1 error』→ 5 条生成用例根本未执行即熔断。
    加该 flag 后收集错误单列（仍计入 failed 供归因，诚实性不变），其余测试正常执行。

    Loop-9（三角色实验 30 题复核实锤）：自带 tests 的**恒定失败**混进 R-Loop 后被"补测修复"
    （永远修不好→熔断/预算耗尽）。修复：round_no>1 起 cmd 限定生成物+imported 文件——
    R-Loop 的 passed/failed 只衡量生成用例质量；首轮保持全目录（自带测试基线）。"""
    cmd = ["pytest", "-q", "--tb=short", "-rfE", "--continue-on-collection-errors",
           "--cov=.", "--cov-report=term-missing:skip-covered",
           # 任务C：逐用例结果（junitxml→test_case_results 表，实时看板数据源）；
           # 与 stdout 解析互不影响（--junitxml 只写文件）；超时/崩溃无文件=诚实缺席
           f"--junitxml={config.JUNIT_PATH}"]
    # 任务3（2026-11-22）per-test 粒度超时：>0 注入 pytest-timeout。方法=signal（实测
    # 反转：thread 法超时即 os._exit，junit 永不落盘=证据蒸发复发；signal 法 SIGALRM
    # 真打断死循环，挂死条 failed+Timeout 进 junit message，其余用例照常出结果——
    # 容器内 docker exec 主线程场景实测 1 failed+9 passed in 3.04s+junit 落盘）。
    # 0=关=命令逐字节不变。
    if config.PER_TEST_TIMEOUT_S > 0:
        cmd += ["--timeout", str(config.PER_TEST_TIMEOUT_S),
                "--timeout-method", "signal"]
    if round_no > 1 and gen_files:
        cmd += gen_files
    return cmd


def build_run_cmd_js(round_no: int = 1, gen_files: list[str] | None = None) -> list[str]:
    """JS 执行链路（Tier2）jest 命令：--json 供宿主侧适配器转 pytest 文本，--coverage 取
    istanbul coverageMap；--ci 关交互 / 快照写入。首轮全目录（自带 tests 基线，同 Python
    口径）；round>1 位置参数限定生成物（jest 把位置参数当 testPathPattern）——Loop-9 同语义。
    --ignore-scripts 类安全：jest 不执行 package 脚本；依赖安装阶段已 --ignore-scripts。
    TS 同走此命令（白包阶段1）：ts-jest 输出仍是 jest JSON，js_adapter 零改动。"""
    cmd = ["jest", "--json", "--coverage", "--ci", "--silent", "--forceExit",
           "--testLocationInResults"]
    if round_no > 1 and gen_files:
        cmd += gen_files
    return cmd


def build_run_cmd_java(ws: Path | None = None) -> list[str]:
    """Java 执行链路（Tier2，白包阶段2）：mvn test 跑全项目（surefire+jacoco，
    依赖随项目走）。round>1 位置参数不适用（surefire 扫全 test 类）——R-Loop 限定
    生成物语义由 gen 侧保证（首轮 TestGenR1，补测轮 TestGenR2 独立类共存，自带
    tests 第 2 轮起不隔离属已知边界：maven 无 jest testPathPattern 等价物，诚实入档）。
    2026-11-16 存疑 35 全修：命令行注入 jacoco 插桩+报告（goal 带版本直调，无需项目
    pom 声明）——1162 实锤 52 过 0 败但 coverage=None。
    2026-11-16 全语言 18 格实锤（1174 java-shop）：**项目 pom 自带 jacoco 时命令行
    再注入 0.8.12 = 双 -javaagent 同 destfile → forked VM exit 134 崩溃 → surefire
    零报告 → repair_env 三轮熔断**。修=探测 pom 含 jacoco 时用项目版本（无版本前缀
    goal 调用取 pom 声明版本，真机验证 EXIT=0+报告完整）；无 jacoco 才注入 0.8.12。"""
    has_jacoco = False
    pom = (ws / "pom.xml") if ws else None
    if pom and pom.exists():
        try:
            has_jacoco = "jacoco" in pom.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass
    if has_jacoco:
        return ["sh", "-c",
                "mvn -q test org.jacoco:jacoco-maven-plugin:report 2>&1"]
    return ["sh", "-c",
            "mvn -q org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent test "
            "org.jacoco:jacoco-maven-plugin:0.8.12:report 2>&1"]


def build_run_cmd_php(gen_files: list[str]) -> list[str]:
    """PHP 执行链路（Tier2，白包阶段4）：phpunit 显式文件清单（TestGen*.php 不匹配
    phpunit 默认 Test.php 后缀发现规则，必须显式传）→ --log-junit（surefire 同 family，
    复用 parse_junit_xml）+ --coverage-clover（pcov 驱动）。自带 tests 第 2 轮起不隔离
    =java/go 同语义边界，诚实入档。"""
    # --coverage-filter 必须显式给（phpunit 11 无 filter 配置时跳过 coverage——真机实锤；
    # "." = 项目根，vendor 由 phpunit 默认排除）
    return ["vendor/bin/phpunit", *gen_files, "--coverage-filter", ".",
            "--log-junit", "/tmp/php_junit.xml",
            "--coverage-clover", "/tmp/php_clover.xml"]


def build_run_cmd_go() -> list[str]:
    """Go 执行链路（Tier2，白包阶段3）：go test -json -coverprofile ./...（stdlib 零网络；
    事件流经 go_adapter 转 pytest 文本，下游零改动）。round>1 位置参数不适用（go test
    无文件级隔离参数，只有包 pattern）——R-Loop 轮全包重跑与 java 同语义边界，诚实入档。"""
    return ["sh", "-c", "go test -json -coverprofile=/tmp/go_cover.out ./... 2>&1"]


def php_exit_for_decide(lang: str, exit_code: int, passed: int, failed: int) -> int:
    """PHP 退出码语义归一（2026-11-15 卡 2，1149 实锤：17 过 0 败 exit≠0 → classify
    兜底 env → repair_env 三轮同指纹熔断）。phpunit 的非零退出码涵盖 warnings/risky/
    覆盖率警告（0=无失败无警告≠pytest 语义）——按失败计数判别：0 败=通过。
    仅 PHP 且确有通过、零失败时清零；其余语言/形态逐字节不变。"""
    if lang == "php" and exit_code != 0 and passed > 0 and failed == 0:
        return 0
    return exit_code


def php_platform_gate_result(exit_code: int, gate_text: str) -> str | None:
    """PHP 平台闸判定（1148 实锤：composer.lock 要求 PHP>=8.4.1 而镜像 8.3.36——
    phpunit 在 platform_check.php 即 Fatal，三轮 repair_env 空转熔断。同 Go 工具链闸
    语义：版本/扩展是环境事实，repair 修不动，开考前确定性拦停不烧修复轮次）。
    exit≠0 → 返回 stop 理由（截取 failed 行给用户）；exit=0 → None 放行。"""
    if exit_code == 0:
        return None
    failed_lines = [ln for ln in (gate_text or "").splitlines() if "failed" in ln.lower()]
    # composer 输出是宽列对齐（failed 在行尾 120+ 列）——压缩空白再截，保关键词可见
    detail = "；".join(" ".join(ln.split())[:200] for ln in failed_lines[:3]) \
        or (gate_text or "").strip()[:200]
    return (f"PHP 平台要求不满足（composer check-platform-reqs exit={exit_code}）：{detail}"
            "——镜像 PHP 版本/扩展与项目 composer.lock 不匹配，诚实拦停（环境事实，repair 修不动）")


def php_unified_with_raw(unified_text: str, raw_stdout: str, raw_stderr: str,
                         has_junit: bool) -> str:
    """PHP run 节点证据保留（1148 实锤证据蒸发家族：phpunit Fatal（平台检查/语法错/
    vendor 缺失 exit 127）只在原始 stdout/stderr，junit 缺失时适配器统一文本把它
    覆盖掉——evaluate·raw 只看到 "0 passed" 假象，真实死因（如 Composer detected
    issues in your platform: require PHP >= 8.4.1）蒸发）。junit 存在=phpunit 正常
    跑完，统一文本即真证据，不附加；junit 缺失=phpunit 没跑成，原始输出尾部是唯一
    死因证据，并入统一文本尾部。"""
    if has_junit:
        return unified_text
    raw = ((raw_stdout or "") + ("\n" + raw_stderr if (raw_stderr or "").strip() else "")).strip()
    if not raw:
        return unified_text
    return unified_text.rstrip("\n") + "\n[phpunit·raw] junit XML 缺失（phpunit 未跑成），" \
           "原始输出尾部：\n" + raw[-600:] + "\n"


def extract_service_port(ws: Path, lang: str) -> int | None:
    """从生成用例文件解析被测服务目标端口（repair_env 起服务用，2026-11-15 存疑 33/34
    拍板全修）。用例里写的端口就是服务应起的端口——LLM 生成时约定俗成（8080/8000），
    从证据取比预设可靠；多端口取出现最多者。解析不到返回 None（fail-open）。"""
    import collections
    if lang == "php":
        pats = ("TestGen*.php", "test_imported.php")
    elif lang == "java":
        pats = ("src/test/java/**/*.java",)
    elif lang in ("javascript", "typescript"):
        pats = ("test_gen_r*.js", "test_gen_fb*.js", "test_gen_*.test.js",
                "test_imported.test.js")
    else:
        return None
    hits: collections.Counter[int] = collections.Counter()
    for pat in pats:
        for p in Path(ws).glob(pat):
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{2,5})", t):
                hits[int(m.group(1))] += 1
            # 分离参数形态（2026-11-16 全语言实锤 1202）：js 用例 {host:'127.0.0.1',
            # port: 8080}——host 与 port 不连写
            for m in re.finditer(r"['\"](?:127\.0\.0\.1|localhost)['\"]\s*,\s*port:\s*(\d{2,5})", t):
                hits[int(m.group(1))] += 1
    return hits.most_common(1)[0][0] if hits else None


_HTTP_CLIENT_RE = re.compile(r"curl_init|fetch\(|axios|supertest|http://|https://")


def detect_api_blackbox(ws: Path, lang: str) -> bool:
    """API 型黑盒识别（存疑 35② 拍板口径）：生成用例走真 HTTP 客户端
    （curl_init/fetch/axios/supertest/URL 直写）→ 被测服务在另一进程，
    插桩（istanbul/clover）结构性测不到。仅 js/ts/php 适用（python 的
    TestClient 进程内形态不受此限）。读全文件——HTTP 特征在文件尾部不漏检
    （2026-11-19 复盘边界洞：旧版只读前 8000 字符）。"""
    if lang not in ("javascript", "typescript", "php"):
        return False
    for pat in ("test_gen_r*.js", "test_gen_fb*.js", "TestGen*.php",
                "test_imported.test.js", "test_imported.php"):
        for gf in Path(ws).glob(pat):
            try:
                text = gf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _HTTP_CLIENT_RE.search(text):
                return True
    return False


def _norm_ep_path(path: str) -> str:
    """端点路径归一化（对账两侧同规则）：去尾斜杠；模板段（<id>/{x}）与纯数字段
    归一为 {p}。已知 v1 边界：非数字非模板段（如 /books/abc 的非法 id 测试）不计入
    已测——只多一轮 repair_gen 且轮次预算兜底，不产生错误 stop（2026-11-19 复盘）。"""
    segs = []
    for s in (path or "").strip().strip("/").split("/"):
        if not s:
            continue
        if (s.startswith("<") and s.endswith(">")) or (s.startswith("{") and s.endswith("}")) \
                or s.isdigit():
            segs.append("{p}")
        else:
            segs.append(s.lower())
    return "/" + "/".join(segs)


def _url_to_path(url: str) -> str:
    """curl_init('http://127.0.0.1:8080/books?x=1') → 归一化 '/books'。"""
    rest = (url or "").partition("://")[2]
    parts = rest.split("/", 1)
    path = "/" + parts[1] if len(parts) > 1 else "/"
    return _norm_ep_path(path.split("?")[0])


def _endpoint_calls_in_tests(ws: Path, lang: str) -> set:
    """从生成用例提取实际调用的 {"METHOD /path"}（黑盒出口对账用，确定性正则）。

    形态取证（task_1981 真机实证）：php=curl_init('http://127.0.0.1:8080/books')+
    CURLOPT_CUSTOMREQUEST => 'POST'（相邻配对：两 init 之间无 CUSTOMREQUEST 默认
    GET）；js/ts=supertest/fetch 的 .get('http://…/path')。返回归一化集合。"""
    pats = ("test_gen_r*.php", "test_gen_fb*.php", "test_imported.php",
            "test_gen_r*.js", "test_gen_fb*.js", "test_imported.test.js") \
        if lang == "php" else \
        ("test_gen_r*.js", "test_gen_fb*.js", "test_gen_r*.ts", "test_gen_fb*.ts",
         "test_imported.test.js", "test_imported.test.ts")
    calls = set()
    for pat in pats:
        for gf in Path(ws).glob(pat):
            try:
                text = gf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if lang == "php":
                inits = [(m.start(), _url_to_path(m.group(1)))
                         for m in re.finditer(r"""curl_init\(\s*['"](http://[^'"]+)['"]""", text)]
                methods = [(m.start(), m.group(1).upper())
                           for m in re.finditer(r"""CURLOPT_CUSTOMREQUEST\s*=>\s*'([A-Z]+)'""", text)]
                for i, (pos, path) in enumerate(inits):
                    nxt = inits[i + 1][0] if i + 1 < len(inits) else len(text)
                    meth = next((m for p2, m in methods if pos < p2 < nxt), "GET")
                    calls.add(f"{meth} {path or '/'}")
            else:
                for m in re.finditer(
                        r"""\.(get|post|put|delete|patch)\(\s*['"]http://[^'"]*?(/[^'"]*)?['"]""",
                        text, re.I):
                    path = _norm_ep_path((m.group(2) or "/").split("?")[0])
                    calls.add(f"{m.group(1).upper()} {path}")
    return calls


def api_blackbox_stop(blackbox: bool, verdict: str, failed: int, exit_code: int,
                      passed: int, coverage: float | None,
                      endpoint_cov: dict | None = None
                      ) -> tuple[str, str, str] | None:
    """黑盒全过且 decide 因覆盖率卡住给 repair_gen → 覆盖为诚实 stop 声明。
    有失败用例（failed>0）不适用——正常归因分诊优先；非黑盒/非 repair_gen 返回 None。

    2026-11-19 复盘升级（"接口级为准"从宣称变核实）：endpoint_cov={"covered",
    "total","missed"} 由调用方算好——
    - 端点全覆盖（missed 空）→ stop，消息带 "端点 C/T 全覆盖"（对账数字可审计）；
    - 有缺口 → 返回 None（维持 repair_gen 补测一轮，缺口清单由调用方消息留痕），
      轮次预算天然兜底；endpoint_cov=None（无注册端点清单）→ 旧行为逐字节不变。"""
    if not blackbox or verdict != "repair_gen" or failed != 0 or exit_code != 0:
        return None
    cov_note = ""
    if endpoint_cov and endpoint_cov.get("total"):
        covered, total = endpoint_cov["covered"], endpoint_cov["total"]
        if endpoint_cov.get("missed"):
            return None            # 端点没测全——不值得 stop，补测一轮（缺口消息由调用方留痕）
        cov_note = f"；端点 {covered}/{total} 全覆盖"
    return ("stop", "none",
            f"API 型黑盒覆盖率不可达（用例真 HTTP，被测服务进程不在插桩范围）——"
            f"用例 {passed} 过 0 败，接口级通过数为准（存疑 35② 拍板口径{cov_note}）；"
            f"覆盖率 {coverage if coverage is not None else 'N/A'} 不作准出判据")


def _php84_needed(ws: Path) -> bool:
    """composer 声明是否要求 PHP>=8.4。候选=ws 根与一层子目录（test program 实测
    形态：demo-main（php_test1）/demo-main/composer.lock 嵌套一层）；lock 的
    依赖约束优先，json 兜底。形态 "^8.4"/">=8.4"（多选一约束里出现即算——
    平台闸按最高要求判定，混约束项目 php83 实测必炸 1148）。"""
    candidates: list[Path] = []
    bases = [Path(ws)]
    if Path(ws).is_dir():
        bases += [c for c in sorted(Path(ws).iterdir())[:20] if c.is_dir()]
    for base in bases:
        for fname in ("composer.lock", "composer.json"):
            p = base / fname
            if p.is_file():
                candidates.append(p)
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r'"php"\s*:\s*"[^"]*8\.[4-9]', text):
            return True
    return False


def cache_binds_for(image: str, task_id: int | None = None,
                    trusted: bool = True) -> dict | None:
    """组装镜像的构建缓存 bind mount（2026-11-15 卡 4，冷缓存家族总解）：
    go126 挂 GOMODCACHE/GOCACHE、jdk17-maven 挂本地 .m2——fresh 重建/跨任务共享
    缓存，二轮构建秒级（1139 gin-master 依赖树 600s 超时×fresh 清零的根治）。
    宿主目录在 data/build_cache/ 下按用途分子目录，缺目录即建。非目标镜像返回
    None（python/js/php 无构建缓存需求，零行为变化）。
    D2（2026-11-20 设计方案）：trusted=False（不可信仓库模式）时写缓存按任务
    隔离——data/build_cache/<sub>/task_<id>/（回炉复用同任务，跨任务零共享=
    缓存投毒免疫）。当前语料全部自选可信开源仓库（默认 trusted=True 行为与历史
    逐字节一致）；接入不可信仓库时按项目标记传 trusted=False。"""
    pairs = config.BUILD_CACHE_BINDS.get(image)
    if not pairs:
        return None
    binds = {}
    for cpath, sub in pairs:
        host = config.BUILD_CACHE_HOST_DIR / sub
        if not trusted and task_id is not None:
            host = host / f"task_{task_id}"
        host.mkdir(parents=True, exist_ok=True)
        binds[str(host)] = {"bind": cpath, "mode": "rw"}
    return binds


def warm_build_cache(runner, container, lang: str) -> str | None:
    """provision 预热步（卡 4 第二半）：首轮把依赖树拉进 bind mount 缓存（900s
    fail-open），后续轮次/任务命中缓存秒级。失败如实留痕不阻断主链路。返回留痕
    消息（None=非 go/java 不适用）。"""
    if lang == "go":
        cmd, label = ["sh", "-c", "cd /work && go mod download 2>&1"], "go mod download"
    elif lang == "java":
        cmd, label = ["sh", "-c", "cd /work && mvn -q dependency:go-offline "
                                    "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent 2>&1"], \
            "mvn dependency:go-offline + jacoco plugin 预热"
    else:
        return None
    r = runner.exec_in(container, cmd, timeout_s=900)
    # 输出截尾在宿主侧做（2026-11-17 multi60 卡 2 实锤 1272-1278：旧版 `| tail -3` 管道
    # 让 sh 的退出码变成 tail 的 0，mvn/go 失败被吞成"预热完成"——缓存预热谎报，
    # MissingProjectException 家族的第二层掩护）
    tail = ((r.stdout or "") + (r.stderr or "")).strip()[-200:]
    if r.exit_code == 0:
        return f"[provision·cache] 构建缓存预热完成（{label}，依赖已入持久缓存）"
    return (f"[provision·cache] 预热未完成（{label} exit={r.exit_code}）——{tail}"
            "（fail-open，不阻断）")


_CONNECTION_FAIL_RE = re.compile(
    r"connection refused|ConnectException|ECONNREFUSED"
    r"|asserting that 0 is identical to \d+", re.I)


def start_service_for_repair(runner, container, ws: Path, lang: str,
                             verdict: str | None, evidence: str) -> str | None:
    """repair_env 回炉轮的连接类失败兜底（2026-11-15 存疑 33/34 拍板全修）：
    L1 对 API 型项目无服务编排——python/go 用例是进程内形态（TestClient/httptest）
    天然自含，java/php 用例走真 HTTP 而 L1 没人起服务 → ConnectException（java，
    归 env 正确但 repair 修不动）/"asserting that 0 is identical to 200"（php，
    curl code=0 曾误归产品缺陷）。本函数在 repair_env 回炉 provision 时：
    连接类失败 + 用例可解析目标端口 → 容器内起被测服务（复用 L2 真机验证过的
    启动器）→ 本轮重跑拿到真实响应。fail-open：不适用/解析不到/启动失败都如实
    留痕继续，不引入新失败面。返回留痕消息（None=不适用）。"""
    if lang not in ("php", "java", "javascript", "typescript") or verdict != "repair_env":
        return None
    if not _CONNECTION_FAIL_RE.search(evidence or ""):
        return None
    port = extract_service_port(ws, lang)
    if port is None:
        return "[provision·service] 连接类失败但用例中未解析到目标端口——跳过起服务（fail-open）"
    from .l2 import (_detect_java_main, _detect_js_entry, _start_service_java,
                     _start_service_js, _start_service_php)
    if lang == "php":
        ok, why = _start_service_php(runner, container, port)
        label = f"php -S 127.0.0.1:{port} index.php"
    elif lang in ("javascript", "typescript"):
        # js/ts 补（2026-11-16 全语言 18 格实锤 1168 js-api-book）：js 生态同样有
        # API 型项目（node http 服务），生成用例真 HTTP → ECONNREFUSED 同族
        entry = _detect_js_entry(ws)
        if not entry:
            return "[provision·service] 连接类失败但未探测到 JS 服务入口——跳过起服务（fail-open）"
        ok, why = _start_service_js(runner, container, entry, port)
        label = f"node {entry}@{port}"
    else:
        main_cls = _detect_java_main(ws)
        if not main_cls:
            return "[provision·service] 连接类失败但未探测到 Java 服务主类——跳过起服务（fail-open）"
        ok, why = _start_service_java(runner, container, main_cls, port)
        label = f"spring-boot:run {main_cls}@{port}"
    if ok:
        return (f"[provision·service] 连接类失败 → 已起被测服务（{label}），"
                "本轮重跑用例将拿到真实响应")
    return f"[provision·service] 服务启动失败（{label}）：{(why or '')[:150]}——如实留痕继续（fail-open）"


def _tier_strategy(primary: str) -> str:
    """语言分级（analyze 用，Loop 抽出供单测）：full=容器执行链路；其余 static_only。
    typescript 升 Tier2（白包阶段1）；java 接线（白包阶段2）→ full；go 接线（白包阶段3）→ full。"""
    return "full" if primary in ("python", "javascript", "typescript", "java", "go", "php") \
        else "static_only"


def _inline_jest_config(include_ts: bool) -> str:
    """散装项目内联 jest 配置（无 jest.config/package.json 时的兜底，853 实锤先例）。
    include_ts=True（白包阶段1）：testMatch 加 .test.ts + ts-jest transform——
    全局镜像已备 ts-jest（NODE_PATH 可达），散装 TS 不需要项目级配置即可执行。
    2026-11-18 multi60 1664 实锤：生成 TS 用例报 TS2304 'expect' not defined——
    ts-jest 默认做类型检查而 @types/jest 的全局 expect 声明在 --config 内联模式
    下不生效 → 关 diagnostics（只转译不查类型，运行时全局 expect 由 jest 提供）。"""
    cfg: dict = {"testMatch": ["**/*.test.js"], "testEnvironment": "node",
                 "moduleFileExtensions": ["js", "json"]}
    if include_ts:
        cfg["testMatch"] = ["**/*.test.ts", "**/*.test.js"]
        cfg["moduleFileExtensions"] = ["ts", "js", "json"]
        cfg["transform"] = {"\\.[jt]sx?$": ["ts-jest", {
            "tsconfig": {"esModuleInterop": True},
            "diagnostics": False}]}
    return json.dumps(cfg)


def _pkg_declares_jest(ws: Path) -> bool:
    """package.json 是否内嵌 jest 配置键（config-as-package 形态，注入判定用）。
    必须 JSON 解析判顶层键——子串判定被 scripts 值误命中（ts-calc 真机实锤：
    "test": "jest" 含 "jest" 子串 → 误判有配置 → 不注入内联配置 → 全局 jest
    无配置拒跑 → suite 全挂）。解析失败按无配置处理（fail-open 到内联兜底）。"""
    p = ws / "package.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    return isinstance(data, dict) and "jest" in data


_SCAN_RESTRICT_KEYS_RE = re.compile(r"\b(testMatch|roots)\b")
_ROOTDIR_RE = re.compile(r"rootDir['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]")


def _jest_config_restricts_scan(ws: Path) -> bool:
    """项目 jest 配置是否限定扫描范围（2026-11-17 multi60 卡 2 补充取证，1291 nestjs
    实锤：项目 jest.config 的 testMatch 只扫 src/ → 写在工作区根的生成物永远
    'No tests found' → 0/0 熔断——嵌套根修复装上依赖后仍会死在这一步）。
    判定=配置声明了 testMatch/roots（替换默认 **/*.test.[jt]s），或 rootDir 指向
    子目录。粗粒度文本判定（JS 配置不 parse），宁可误注入内联配置（行为可见有
    留痕）也不漏盲区；无配置/纯 transform 配置返回 False。"""
    names = ("jest.config.js", "jest.config.ts", "jest.config.mjs",
             "jest.config.cjs", "jest.config.json")
    text = ""
    for n in names:
        p = ws / n
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            break
    if not text and _pkg_declares_jest(ws):
        try:
            data = json.loads((ws / "package.json").read_text(encoding="utf-8",
                                                              errors="ignore"))
            text = json.dumps(data.get("jest") or {}, ensure_ascii=False)
        except (json.JSONDecodeError, OSError, ValueError):
            text = ""
    if not text:
        return False
    if _SCAN_RESTRICT_KEYS_RE.search(text):
        return True
    for m in _ROOTDIR_RE.finditer(text):
        if m.group(1) not in (".", "./", ""):
            return True
    return False


def _js_needs_inline(ws: Path, lang: str) -> bool:
    """run 节点 jest 内联配置注入判定（纯函数，单测锁）。
    三档：①无任何配置 → TS/散装形态注入（853 先例）②有配置但限定扫描范围
    （testMatch/roots/rootDir 子目录）→ 注入覆盖（1291 nestjs 盲区，2026-11-17
    并行取证会商修复方向）③有配置且不限定 → 走项目自己的配置（零行为变化）。"""
    has_cfg = any((ws / n).exists() for n in
                  ("jest.config.js", "jest.config.ts", "jest.config.mjs",
                   "jest.config.cjs", "jest.config.json")) or _pkg_declares_jest(ws)
    if has_cfg:
        return _jest_config_restricts_scan(ws)
    return lang == "typescript" or not (ws / "package.json").exists()


def _validate_repo_url(src: str) -> None:
    """Loop-6/TODO-13（体检红队四问降级项）：repo_url 白名单校验——git clone 的 src
    以位置参数传入，option 形态注入实测不执行但防御性收敛：只允许 http(s)/git@/ssh URL
    或本地存在路径；含空白/引号/Option 前缀（-）/shell 元字符一律 RuntimeError。"""
    if os.path.isdir(src):
        return
    if src.startswith("-") or re.search(r"[\s\"';&|`$<>]", src):
        raise RuntimeError(f"repo_url 非法（含空白/引号/shell 元字符或 Option 前缀）：{src[:80]}")
    # 形似本地路径但目录不存在（2026-11-17 用户实锤 18 例：路径少打一段被误报
    # "格式非法"，误导排查方向）——单独报"路径不存在"并给出最近似目录提示
    if re.match(r"^[A-Za-z]:[\\/]|^/", src):
        parent = os.path.dirname(src.rstrip("\\/"))
        hint = f"（上级目录存在：{parent}——请检查是否少打了子目录）" if os.path.isdir(parent) else ""
        raise RuntimeError(f"本地路径不存在：{src[:120]}{hint}")
    if not re.match(r"^https?://|^git@|^ssh://", src):
        raise RuntimeError(f"repo_url 非法（仅支持 http(s)/git@/ssh URL 或本地路径）：{src[:80]}")
    if re.match(r"^https?://[^/@]+:[^/@]*@", src):
        # 带 userinfo 的 URL 会把口令原样入库（projects.repo_url），克隆失败时 git
        # stderr 还会回显到任务 error——fail-closed 拒绝（2026-11-15 闲时找bug）
        raise RuntimeError("repo_url 不得携带账号口令（userinfo）——请用匿名 https 或凭据助手")


def _extract_tarball_strip1(data: bytes, dest: Path) -> int:
    """GitHub tarball（顶层 <repo>-<branch>/）解包到 dest，剥顶层目录；返回落盘文件数。

    逐成员手写落盘而非 extractall：要剥前缀，且只收常规文件、拒绝路径穿越。"""
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if len(parts) < 2:
                continue
            target = dest.joinpath(*parts[1:]).resolve()
            if dest.resolve() not in target.parents:
                continue  # 路径穿越（../ 等）直接丢弃
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(member)
            if f is None:
                continue
            target.write_bytes(f.read())
            n += 1
    return n


def _codeload_fallback(src: str, ws: Path) -> str | None:
    """git clone 失败时的确定性回退：codeload tarball 直连下载（显式绕代理）。

    调研结论（2026-10-30）：GitPython/dulwich/pygit2 都是 git 协议实现，同样依赖
    网络/代理，没有现成轮子做"git→tarball 回退"，逻辑太贴场景不值得引重依赖。
    仅支持 GitHub 仓库 URL；main/master 双分支尝试；失败返回 None 由调用方如实报错。"""
    m = _GH_URL.match(src.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # 显式空 ProxyHandler：代理失效环境里 codeload 直连可达（2026-10-30 实测），
    # 不继承系统/环境代理配置
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for branch in ("main", "master"):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        try:
            with opener.open(urllib.request.Request(
                    url, headers={"User-Agent": "test-agent"}), timeout=120) as r:
                data = r.read()
            n = _extract_tarball_strip1(data, ws)
            if n:
                return f"codeload tarball（{branch} 分支，{n} 个文件）"
        except Exception as e:  # 网络/解包失败换下一分支，全败返回 None
            log.warning("codeload 回退失败 %s: %s", url, e)
            continue
    return None


@dataclass
class GraphDeps:
    conn: object
    runner: SandboxRunner
    registry: SkillRegistry
    skills: dict


def make_deps() -> GraphDeps:
    conn = get_conn()
    runner = SandboxRunner()
    registry = SkillRegistry(conn)
    # 注册表幂等 upsert（A13）：skills 表缺行会让审计台账的 INSERT..SELECT 插 0 行，
    # 论文"工具调用成功率"指标就只剩 run_tests 一个工具——全部 skills 必须注册
    from .l2 import build_l2_skills
    skills = {**build_skills(runner), **build_eval_skills(), **build_l2_skills(runner)}
    from ..skills.static_scan import _static_scan_skill
    skills["static_scan"] = _static_scan_skill()
    for s in skills.values():
        registry.register(s)
    return GraphDeps(conn=conn, runner=runner, registry=registry, skills=skills)


def set_status(conn, task_id: int, status: str) -> None:
    conn.execute("UPDATE test_tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                 (status, task_id))
    conn.commit()


_ROW = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+([\d,\s\-]+)$")
# 适配器 3 列格式（go/java/php 统一文本：file miss pct% missing）——F2 阶段 1 #1：
# 旧 _ROW 只认 pytest 4 列 → 三语言 per_file 恒空 → C3 缺口回灌从未生效（跨语言修）
_ROW_ADAPTER = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)%\s+([\d,\s\-]+)$")


def parse_conditions(text: str) -> list[dict] | None:
    """从 LLM 回复中鲁棒提取测试条件；解析失败返回 None（诚实降级，不硬造条件）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    conds = data.get("conditions")
    if not isinstance(conds, list):
        return None
    cleaned = [c for c in conds if isinstance(c, dict) and c.get("condition")]
    return cleaned or None


def _parse_per_file_coverage(stdout: str) -> list[dict]:
    """term-missing 逐文件覆盖明细（B2）：只留有缺口的文件行，供缺口回灌与趋势图。
    双格式（F2 阶段 1 #1）：pytest 4 列（file stmts miss cover% missing）优先；
    go/java/php 适配器 3 列（file miss pct% missing）兜底——4 列先试防歧义
    （3 列正则对 4 列行不匹配：第二数字后是空格非 %）。"""
    rows = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(("Name", "TOTAL")):
            continue
        m = _ROW.match(line)
        if m:
            rows.append({"file": m.group(1), "stmts": int(m.group(2)),
                         "miss": int(m.group(3)), "cover": int(m.group(4)),
                         "missing": m.group(5).strip()})
            continue
        m = _ROW_ADAPTER.match(line)
        if m:
            rows.append({"file": m.group(1), "stmts": None,
                         "miss": int(m.group(2)), "cover": int(m.group(3)),
                         "missing": m.group(4).strip()})
    return rows


def _try_recipe_image(state, tid: int, runner, msg) -> str | None:
    """任务F：探测→确定性配方→构建+自检，成功返回镜像 tag，失败返回 None（回退预置镜像）。

    全程落 image_recipes 台账（配方原文/来源/阶段/错误）——供应链可审计。
    任何异常都只 WARN 不上抛：环境增强失败不得拖垮任务。
    """
    try:
        from ..skills.recipe import try_build_recipe
        from ..skills.stack_detect import detect_stack, deterministic_recipe
        ws = Path(state["workspace"])
        stack = detect_stack(ws)
        rec = deterministic_recipe(stack)
        if not rec:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 探测结果不足以生成配方"
                f"（语言={stack.get('language')} 版本={stack.get('runtime_version')}）→ 用预置镜像")
            return None
        if stack.get("conflicts"):
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 探测注意：{'；'.join(stack['conflicts'])}")
        res = try_build_recipe(rec, runner)
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO image_recipes(task_id, source, recipe, image_tag, status, stage, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid, rec.get("source", "deterministic"), json.dumps(rec, ensure_ascii=False),
                 res.get("tag"), "built" if res["ok"] else "failed",
                 res.get("stage"), (res.get("error") or "")[:800] or None))
            conn.commit()
        finally:
            conn.close()
        if res["ok"]:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 环境配方构建通过（{rec['language']} "
                f"{rec['runtime_version']} / {rec['package_manager']}）→ 镜像 {res['tag']}"
                f"；依据：{', '.join(rec['evidence']['detected_from'][:4]) or '—'}")
            return res["tag"]
        msg(state["session_id"], "assistant",
            f"[provision·recipe] 配方在 {res.get('stage')} 阶段失败，回退预置镜像："
            f"{(res.get('error') or '')[:200]}")
        return None
    except Exception as e:      # noqa: BLE001 —— 环境增强失败不拖垮任务
        log.warning("recipe 流程异常 task=%s: %s: %s", tid, type(e).__name__, e)
        try:
            msg(state["session_id"], "assistant",
                f"[provision·recipe] 流程异常（{type(e).__name__}），回退预置镜像")
        except Exception:       # noqa: BLE001
            pass
        return None


def prepare_task_image(runner, image: str, auto: bool) -> tuple[bool, str]:
    """沙箱自适应的镜像门（模块级纯函数便于单测）：

    auto=True（模式③一条龙 / AUTO_PREPARE_ENV / 任务勾选）→ 缺镜像自动构建（ensure_image）；
    auto=False（模式①手动感知）→ 只探测不构建，缺失时返回 False 由 provision 诚实 stop。
    返回 (是否就绪, 来源说明)。
    """
    if auto:
        if runner.ensure_image(image):
            return True, "本地已有或已自动构建"
        return False, "自动构建失败（详见服务端日志）"
    if runner.image_exists(image):
        return True, "本地已有"
    return False, "本地缺失（未开启自动准备）"


def _force_rmtree(path) -> None:
    """Windows 兼容删除：git 把 loose object 置只读，裸 rmtree 在 Windows 删只读文件
    直接 PermissionError——git 任务重跑必炸（2026-11-15 闲时找bug）。onerror 清
    只读属性后重试（Python 官方 recipes 同款）。"""
    import stat

    def _onerr(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerr)


def _git_clone(src: str, ws) -> object | None:
    """git clone --depth 1（抽纯函数便于超时语义单测）。超时（挂起型代理）返回
    None 与 returncode!=0 同路——codeload 回退正是为代理失效场景设计的，旧实现
    TimeoutExpired 直接上抛永远走不到回退（2026-11-15 闲时找bug）。"""
    try:
        return subprocess.run(["git", "clone", "--depth", "1", src, str(ws)],
                              capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None


def _pick_primary(counts: dict[str, int]) -> str:
    """主语言确定性选择：LOC 降序、同 LOC 按字母序——rglob 枚举序（重克隆可变）
    不得影响结果，否则重跑 primary 翻转→分级/镜像/报告不可比（2026-11-15 闲时找bug）。"""
    if not counts:
        return "unknown"
    return sorted(counts, key=lambda k: (-counts[k], k))[0]


def make_nodes(deps: GraphDeps) -> dict:
    conn, runner = deps.conn, deps.runner
    registry, skills = deps.registry, deps.skills

    def msg(session_id: int, role: str, content: str) -> None:
        seq = conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                           (session_id,)).fetchone()["n"]
        conn.execute("INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
                     (session_id, seq, role, content))
        conn.commit()

    # ---------- 1. 克隆 ----------
    def clone(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "cloning")
        ws = config.DATA_DIR / "workspaces" / f"task_{tid}"
        if ws.exists():
            _force_rmtree(ws)
        ws.mkdir(parents=True)
        src = state["repo_url"]
        _validate_repo_url(src)  # Loop-6/TODO-13：非法 URL/路径 fail-closed
        if os.path.isdir(src):
            # 本地路径：copytree 本身尊重源项目 .gitignore（晚间实锤 348：项目 .gitignore
            # 已排除 data/ 7.4GB，但此前的裸 copytree 照拷不误，clone 后预算检查又不尊重
            # .gitignore → 把用户按提示做了的事直接否定）——spec 命中目录/文件直接不复制
            from ..sandbox.runner import _load_gitignore_spec
            spec = _load_gitignore_spec(Path(src))
            src_root = Path(src).resolve()

            def _gi_ignore(directory, names):
                rel_base = Path(directory).resolve().relative_to(src_root)
                skip = []
                for n in names:
                    rel = (rel_base / n).as_posix()
                    is_dir = (Path(directory) / n).is_dir()
                    if any(fnmatch.fnmatch(n, pat) for pat in config.SKIP_DIRS):
                        skip.append(n)
                    elif spec is not None and spec.match_file(rel + ("/" if is_dir else "")):
                        skip.append(n)
                return skip

            shutil.copytree(src, ws, dirs_exist_ok=True, ignore=_gi_ignore)
            how = "本地目录快照（尊重 .gitignore）"
        else:
            r = _git_clone(src, ws)
            if r is None or r.returncode != 0:
                # D2（2026-10-30 H1 备料实锤）：git 走系统代理，代理失效时真实 GitHub URL
                # 必炸 env 类错误；codeload tarball 直连可达 → 确定性回退，失败原因如实留痕
                fb = _codeload_fallback(src, ws)
                if fb:
                    msg(state["session_id"], "assistant",
                        f"[clone] git clone 失败（{r.stderr.strip()[-160:]}），回退 {fb}")
                    how = fb
                else:
                    raise RuntimeError(
                        f"git clone 失败且无可用回退（非 GitHub 仓库或分支不可达）: {r.stderr[:400]}")
            else:
                how = "git clone --depth 1"
        # 嵌套项目根上提（2026-11-17 multi60 卡 2，java petclinic/php demo-main/ts nestjs
        # 三族 12 格实锤：codeload zip/多层打包把项目根藏在一层子目录里，install/run/
        # collect 全以 /work 为根跑命令 → MissingProjectException/vendor 缺失/ts_loose
        # 误判 → 0 收集三轮熔断）。歧义布局不猜，如实不上提（见 nested_root 模块头）。
        from ..skills.nested_root import lift_nested_project_root
        try:
            lift_msg = lift_nested_project_root(ws)
            if lift_msg:
                msg(state["session_id"], "assistant", lift_msg)
        except Exception as e:      # noqa: BLE001 —— 上提失败不炸 clone，布局原样继续
            log.warning("嵌套根上提跳过（%s: %s）", type(e).__name__, e)
        # P1-②：重跑 rmtree 后 imported 用例文件无人重建（DB 指标仍统计）——
        # 以库为准重放 test_imported.py，仅当库里确有 enabled=1 的 imported 用例
        from ..skills.gen_tests import _rebuild_imported_file
        n_imported = conn.execute(
            """SELECT COUNT(*) FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
               WHERE s.task_id=? AND c.source='imported' AND c.enabled=1""",
            (tid,)).fetchone()[0]
        if n_imported:
            _rebuild_imported_file(conn, tid, ws)
            msg(state["session_id"], "assistant",
                f"[clone] 从库重建 test_imported.py（imported 用例 {n_imported} 条，重跑语义恢复）")
        # B7 clone 预算：工作区体积超限 fail-fast（不让巨型仓库把磁盘拖满后
        # 在后续阶段以各种奇怪方式失败）；阈值复用任务B 的快照预算
        ws_bytes = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())
        from ..sandbox.runner import _fmt_bytes
        if ws_bytes > config.SNAPSHOT_MAX_TOTAL_BYTES:
            raise RuntimeError(
                f"克隆后工作区 {_fmt_bytes(ws_bytes)} 超过预算 "
                f"{_fmt_bytes(config.SNAPSHOT_MAX_TOTAL_BYTES)}："
                f"请把大目录/大文件加入项目 .gitignore（快照会尊重它），"
                f"或调大 config.SNAPSHOT_MAX_TOTAL_BYTES 后重试")
        msg(state["session_id"], "assistant",
            f"[clone] {how} → {ws}（{_fmt_bytes(ws_bytes)}）")
        return {"workspace": str(ws)}

    # ---------- 2. 分析（语言占比 / 模块分级 / 准出标准冻结） ----------
    def analyze(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "analyzing")
        ws = Path(state["workspace"])
        pid = conn.execute("SELECT project_id FROM test_tasks WHERE id=?", (tid,)).fetchone()["project_id"]
        counts: dict[str, int] = {}
        for p in ws.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _EXT_LANG:
                continue
            # 相对路径 parts（2026-11-15 闲时找bug，db_probe.py:53 同型）：绝对路径
            # parts 含工作区祖先——祖先目录名撞排除集（build/venv/...）即全量误杀
            if any(part in config.SKIP_DIRS for part in p.relative_to(ws).parts):
                continue
            try:
                lang = _EXT_LANG[p.suffix.lower()]
                counts[lang] = counts.get(lang, 0) + sum(1 for _ in p.open(errors="ignore"))
            except OSError:
                continue
        total = sum(counts.values()) or 1
        ratios = {k: round(v / total, 3) for k, v in counts.items()}
        primary = _pick_primary(counts)

        conn.execute("UPDATE projects SET primary_language=?, language_ratios=?, clone_status='ok' WHERE id=?",
                     (primary, json.dumps(ratios, ensure_ascii=False), pid))
        # 分级（2026-11-01 用户拍板；白包阶段1/2026-11-06 升级）：python/javascript=容器执行
        # （Tier1/Tier2）；typescript 升 Tier2（镜像已备 ts-jest，散装走内联 transform 配置，
        # 见 _inline_jest_config）；java 移出"假 full"（镜像已备但生成/执行链路未接，白包阶段2）
        # → 走静态扫描兜底（Tier3）：扫描+测试设计草稿落库，报告明示"未执行"
        strategy = _tier_strategy(primary)
        # upsert：A4 允许重跑 done/failed 任务，重跑时模块行必须幂等（否则 UNIQUE 崩）
        # upsert：A4 允许重跑 done/failed 任务，重跑时模块行必须幂等（否则 UNIQUE 崩）。
        # 注意：冲突走 DO UPDATE 分支时 cur.lastrowid 不是模块行 id（sqlite3 语义=保持
        # 同连接上一条成功 INSERT 的 rowid，重跑时会拿到 session 行 id 指错外键）——
        # 必须回读 SELECT 取真 id（2026-11-15 闲时找bug 第一轮实锤）
        conn.execute(
            """INSERT INTO modules(project_id, path, language, loc, strategy) VALUES (?, '.', ?, ?, ?)
               ON CONFLICT(project_id, path) DO UPDATE SET language=excluded.language,
                 loc=excluded.loc, strategy=excluded.strategy""",
            (pid, primary, total, strategy))
        module_row = conn.execute(
            "SELECT id FROM modules WHERE project_id=? AND path='.'", (pid,)).fetchone()
        # 准出标准冻结进任务行（A1）：此后 decide 只认这个值，实验中途改 config 不影响在途任务
        conn.execute("UPDATE test_tasks SET coverage_target=? WHERE id=?",
                     (config.COVERAGE_TARGET, tid))
        # W4 热点快照 delete-first（与 plan/test_conditions 同款重跑幂等语义）：
        # 无条件清旧——重跑时主语言判定可能变化，残留行会让"热点未覆盖"指标失真
        conn.execute("DELETE FROM complexity_hotspots WHERE task_id=?", (tid,))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[analyze] 语言占比 {ratios} → 主语言 {primary}，分级 {strategy}，"
            f"准出标准冻结：行覆盖率 ≥ {config.COVERAGE_TARGET:.0%} 或预算耗尽")
        out = {"primary_language": primary, "module_id": module_row["id"] if module_row else None,
               "coverage_target": config.COVERAGE_TARGET,
               "image": config.EXEC_LANGUAGES.get(primary, config.IMAGE_PY)}
        # W4 圈复杂度热点（radon 宿主侧确定性扫描）：注入 plan 提示词做风险分级倾斜；
        # 落库 complexity_hotspots（报告"热点未覆盖"指标的数据源，第 17 张表）
        if primary == "python":
            from ..skills.complexity import complexity_hotspots
            hotspots = complexity_hotspots(ws)
            if hotspots:
                out["complexity_hotspots"] = hotspots
                for h in hotspots:
                    conn.execute(
                        """INSERT INTO complexity_hotspots(task_id, file, name,
                                                          complexity, rank, lineno)
                           VALUES (?,?,?,?,?,?)""",
                        (tid, h["file"], h["name"], h["complexity"], h["rank"], h["lineno"]))
                conn.commit()
                msg(state["session_id"], "assistant",
                    "[analyze] W4 复杂度热点：" +
                    "; ".join(f"{h['file']}::{h['name']}={h['complexity']}({h['rank']})"
                              for h in hotspots)[:300])
        # 任务卡-F2 阶段 1 #2/#4（MTA_GO_STATIC 默认关）：Go 静态分析层——gocyclo
        # 热点复用 complexity_hotspots 管线（radon 同尺 rank，plan 提示词风险倾斜零
        # 改动）+ go vet 结构化发现留痕。宿主无 Go 工具链 → analyze 期 best-effort
        # 起短命容器采集（与 radon 缺失同款 fail-open：任何失败=空表不阻断）。
        if primary == "go" and config.GO_STATIC_ENABLED:
            try:
                from ..skills.go_static import collect_go_static, complexity_rank
                _gr = SandboxRunner()
                _name = f"mta-gostatic-{tid}"
                _gr.cleanup(_name)
                _c = _gr.ensure_container(config.IMAGE_GO, _name, fresh=True)
                try:
                    _gr.copy_dir_into(_c, ws)
                    gstatic = collect_go_static(_gr, _c)
                finally:
                    _gr.cleanup(_name)
                cyc_hot = sorted(gstatic["gocyclo"], key=lambda x: -x["complexity"])[:5]
                cyc_hot = [h for h in cyc_hot if h["complexity"] >= 10]   # C 及以上（radon 同尺）
                if cyc_hot:
                    out["complexity_hotspots"] = [
                        {"file": h["file"], "name": h["func"],
                         "complexity": h["complexity"],
                         "rank": complexity_rank(h["complexity"]),
                         "lineno": h["line"]} for h in cyc_hot]
                    for h in out["complexity_hotspots"]:
                        conn.execute(
                            """INSERT INTO complexity_hotspots(task_id, file, name,
                                                              complexity, rank, lineno)
                               VALUES (?,?,?,?,?,?)""",
                            (tid, h["file"], h["name"], h["complexity"],
                             h["rank"], h["lineno"]))
                    conn.commit()
                    msg(state["session_id"], "assistant",
                        "[analyze·go] gocyclo 热点（进 plan 提示词倾斜）：" +
                        "; ".join(f"{h['file']}::{h['name']}={h['complexity']}"
                                  f"({h['rank']})" for h in out["complexity_hotspots"])[:300])
                else:
                    msg(state["session_id"], "assistant",
                        "[analyze·go] gocyclo 无 C 级以上热点（如实空表）")
                if gstatic["vet"]:
                    msg(state["session_id"], "assistant",
                        "[analyze·go] go vet 发现 %d 条（结构化）：" % len(gstatic["vet"]) +
                        "; ".join(f"{v['file']}:{v['line']} {v['message'][:40]}"
                                  for v in gstatic["vet"][:5])[:300])
            except Exception as e:              # noqa: BLE001 —— fail-open 不阻断
                msg(state["session_id"], "assistant",
                    f"[analyze·go] 静态分析失败（{type(e).__name__}: {str(e)[:100]}），主链路继续")
        # 任务卡-E A 版（MTA_MODULE_PLAN 默认关）：模块级画像与策略建议——只"看与
        # 建议"不改执行链。切分（确定性）→9 维画像→规则表→规则未命中 LLM 兜底一次
        # （理由落库可审计）；失败不挡主链路（fail-open 如实留痕）
        if config.MODULE_PLAN_ENABLED:
            try:
                from ..skills.llm_client import make_chat
                from ..skills.llm_usage import log_llm_call

                def _module_llm(prompt: str) -> str:
                    llm = make_chat(phase="module_profile", temperature=0,
                                    timeout=60, max_retries=1)
                    resp = llm.invoke([("user", prompt)])
                    log_llm_call(tid, "module_profile", resp,
                                 ctx_budget=getattr(llm, "_ctx_budget", None),
                                 base_url=getattr(llm, "_base_url", ""))
                    return resp.content

                from ..skills.module_profile import analyze_modules
                from ..skills.module_profile import persist as persist_profiles
                rep = analyze_modules(ws, primary, llm_fn=_module_llm)
                n = persist_profiles(conn, pid, rep)
                msg(state["session_id"], "assistant",
                    f"[analyze·modules] 模块画像 {n} 个落 module_profiles（第 34 表）"
                    + (f"；{rep['note']}" if rep.get("note") else ""))
            except Exception as e:              # noqa: BLE001 —— 画像失败不挡主链路
                msg(state["session_id"], "assistant",
                    f"[analyze·modules] 画像失败（{type(e).__name__}: {str(e)[:120]}），"
                    "主链路继续（如实留痕）")
        # 项目知识索引+需求路由（2026-11-18 设计方案落地）：零 LLM 打标签+别名路由；
        # L1 依赖闭包（辅助层默认开）：命中模块自动加调用图 1 跳邻居（防"bug 在被
        # 调用函数里"漏测）；图建不出（非 Python/JS/Go）自动降级=只锁命中模块
        try:
            from ..skills.project_index import (dependency_closure, ensure_index,
                                                match_requirement, scope_prefixes_for)
            idx_doc = ensure_index(tid, Path(state["workspace"]),
                                   [state.get("primary_language", "")] or ["python"])
            hit = match_requirement(idx_doc, state.get("requirement", ""))
            if hit["kind"] == "module":
                closure = dependency_closure(idx_doc, hit["module"], hops=1,
                                             callgraph=idx_doc.get("callgraph"))
                # A3（2026-11-20）：scope 前缀只从索引真实文件路径推导——修复前用模块
                # 名拼目录（src/user 布局模块名 user → scope=user/ 永远锁空）
                state["index_scope_paths"] = scope_prefixes_for(idx_doc, closure)
                state["index_closure"] = closure
                # 366 教训：直接改 state 不放进返回值=跨节点被 schema 丢弃——必须同时进 out
                out["index_scope_paths"] = state["index_scope_paths"]
                out["index_closure"] = closure
                locked = sum(len((idx_doc.get("modules") or {}).get(m, {}).get("files", []))
                             for m in closure)
                msg(state["session_id"], "assistant",
                    f"[analyze·index] 需求命中模块 {hit['module']}（置信 {hit['confidence']}）"
                    f"+依赖闭包 {len(closure) - 1} 模块（L1）——源码范围已锁定 {locked} 文件")
            elif hit["kind"] == "endpoints":
                state["index_endpoints"] = hit["endpoints"]
                out["index_endpoints"] = hit["endpoints"]
                msg(state["session_id"], "assistant",
                    f"[analyze·index] 需求命中接口清单——共 {hit['count']} 端点已注册（逐端点对账）")
            else:
                msg(state["session_id"], "assistant",
                    "[analyze·index] 需求未命中索引——回退全库扫描（细细找）")
        except Exception as e:  # noqa: BLE001 —— 索引失败不炸 analyze
            log.warning("索引路由失败（%s: %s）——回退全库", type(e).__name__, e)
        if strategy != "full":
            # 多测试路线编排（期 A，MTA_ROUTER 默认关）：static_only 项目不再是死胡同——
            # 确定性路由器按项目特征找可用路线（如纯 HTML 的 UI 交互幂等），落 route_plan
            # 表（每条候选路线的 选/不选+理由，可审计）；找到可用路线则标记 ui_routes 供
            # static_draft 节点走真实执行出口，未找到仍走静态草稿（旧行为不变）
            if getattr(config, "ROUTER_ENABLED", False):
                from ..skills.routes import extract_route_features, route_plan
                feats = extract_route_features(ws)
                plan_map = route_plan(state.get("requirement", ""), feats,
                                      language=primary, conn=conn, task_id=tid,
                                      flags={"l2": config.L2_BLACKBOX_ENABLED,
                                             "l3": config.LEVELS_L3_ENABLED,
                                             "l4": config.LEVELS_L4_ENABLED})
                sel = plan_map["selected_ids"]
                msg(state["session_id"], "assistant",
                    f"[router] 路线编排（期 A）：候选 {len(plan_map['routes'])} 条，"
                    f"通过前置 {len(sel)} 条 → {'、'.join(sel) or '无（仅静态兜底）'}"
                    f"；逐条理由已落 route_plan 表")
                if "ui_interaction_idempotency" in sel:
                    return {**out, "static_only": True, "ui_routes": sel}
            msg(state["session_id"], "assistant",
                f"[analyze] 主语言 {primary} 无容器执行链路 → 静态模式（Tier3 兜底）：确定性扫描 + "
                f"AI 测试设计草稿（不执行、无覆盖率），报告将明示未验证")
            return {**out, "static_only": True}   # 不再 done 短路：走 plan→static_draft→report
        return out

    # ---------- 3. 环境供给（容器 + 快照 + 依赖安装，全部留台账） ----------
    def provision(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "provisioning")
        # 环境 recipe（任务F）：确定性探测→白名单配方→构建+自检。成功=用更贴合项目
        # 版本的镜像；失败=回退预置镜像（老路径不变，不引入新失败面）。开关默认关。
        image = state["image"]
        # PHP 版本自选（2026-11-16 test program 验收）：composer 声明 >=8.4 而镜像
        # php83 缺 8.4 → platform_check 必炸（1148/demo-main 实锤）。php84 基座已建
        # （ARG PHP_BASE），本地在即换用；不在则保持 php83 走平台闸诚实拦停。
        if (state.get("primary_language") == "php" and image == config.IMAGE_PHP):
            swap = _php84_needed(Path(state["workspace"]))
            if swap and runner.image_exists("mta-test:php84"):
                image = "mta-test:php84"
                msg(state["session_id"], "assistant",
                    "[provision] composer 要求 PHP≥8.4——执行镜像换用 mta-test:php84（版本自选）")
        if config.RECIPE_ENABLED:
            image = _try_recipe_image(state, tid, runner, msg) or image
        # 沙箱自适应（设计文档 §6）：镜像按需供给——任务级判定，缺镜像按模式分流
        auto = config.AUTO_PREPARE_ENV or bool(state.get("auto_prepare"))
        ready, how = prepare_task_image(runner, image, auto)
        if not ready:
            reason = ("执行镜像未就绪且未开启自动准备（模式①手动感知）："
                      "请在界面点「一键准备」或到设置页构建镜像，"
                      "或新建任务时勾选「自动准备环境」后重跑")
            msg(state["session_id"], "assistant", f"[provision] {reason}")
            return {"done": True, "verdict": "stop", "stop_reason": reason}
        msg(state["session_id"], "assistant", f"[provision] 执行镜像 {image} 就绪（{how}）")
        # 两阶段"布考场"（docs/两阶段人机协作流程设计-20261031.md）：快照前按审查结论
        # 重写生成用例文件（评审页勾掉/增补的用例以库为准），再补跑阶段一顺延的收集闸
        if state.get("two_stage_rebuild"):
            from ..skills.gen_tests import collect_gate_files, rebuild_gen_files_from_db
            if state.get("verify_mode") == 1:
                # 0-A（2026-11-20 设计稿 §2.1）：验证任务布考场优先按冻结快照逐字节重写
                # ——G9 实锤 DB 重写有 repr 风格差（语义零改字节不同）；快照缺失诚实回退
                # DB 重写并留痕（物化风格差由终态分文件对账豁免判读）
                from ..skills.verify_fix import restore_from_snapshot
                rr = restore_from_snapshot(conn, tid, Path(state["workspace"]))
                if rr["mode"] == "snapshot":
                    msg(state["session_id"], "assistant",
                        f"[verify-freeze] 布考场按冻结快照逐字节重写 "
                        f"{rr['n_files']} 个用例文件（0-A 物化快照）")
                else:
                    n_files = rebuild_gen_files_from_db(conn, tid, Path(state["workspace"]))
                    msg(state["session_id"], "assistant",
                        f"[verify-freeze] snapshot_missing——回退 DB 重写 {n_files} 个"
                        "用例文件（0-A 审计：风格差由终态对账豁免判读）")
            else:
                n_files = rebuild_gen_files_from_db(conn, tid, Path(state["workspace"]))
                if n_files:
                    msg(state["session_id"], "assistant",
                        f"[provision] 布考场：按审查结论重写 {n_files} 个生成用例文件（停用用例已摘除）")
        # 上一轮执行超时（exit=-1）说明容器里有 exec 残留，必须重建（A2），否则新旧任务争抢 CPU
        fresh = state.get("last_exit") == -1 and state.get("round_no", 0) > 0
        # 构建缓存持久化（2026-11-15 卡 4）：go/java 容器挂宿主缓存目录（跨容器/任务
        # 共享，fresh 重建不清零）；cache_binds_for 失败（目录建不了等）→ None 走老路径
        try:
            volumes = cache_binds_for(image, task_id=tid,
                                      trusted=config.BUILD_CACHE_TRUSTED)
        except Exception as e:      # noqa: BLE001 —— 缓存挂载不引入新失败面
            log.warning("构建缓存挂载组装失败（%s: %s），跳过", type(e).__name__, e)
            volumes = None
        container = runner.ensure_container(image, state["container_name"], fresh=fresh,
                                            volumes=volumes)
        # 任务2（2026-11-22）：伪造网络——旗标开时起桩+SUT 双网络挂载+API_BASE_URL 注入。
        # 关=零执行逐字节不变。桩 fail-open（起失败主任务照常）；双网络=
        # bridge 主网保 host.docker.internal MySQL + mta-fakenet 挂桩。
        if config.FAKE_NET_ENABLED:
            from ..sandbox import fakenet
            stub = fakenet.ensure_stub(runner.client, tid)
            if stub:
                try:
                    runner.client.networks.get(config.FAKE_NET_NAME).connect(container.id)
                    msg(state["session_id"], "assistant",
                        f"[fakenet] 伪造网络已挂：桩 http://{stub}"
                        f"（internal 网，外发隔离实证见 scripts/ensure_fake_net.py）")
                    state["fakenet_stub"] = stub
                except Exception as e:  # noqa: BLE001 —— fail-open
                    log.warning("[fakenet] SUT 挂网失败（fail-open）：%s", e)
        # 任务B：快照摘要落 agent_messages——跳过了什么/打了多大，界面可查
        # （#281 教训：7.7GB 工作区静默 OOM，用户只看到 MemoryError 不知道为什么）
        # 缺口②修复（2026-11-05）：差分闸开启时 .git 必须随行进考场，否则
        # run_diff_gate 在容器内恒 no_git（真机验证实锤）；默认关=语义不变
        snap = runner.copy_dir_into(container, state["workspace"],
                                    keep_git=config.DIFF_GATE_ENABLED)
        from ..sandbox.runner import _fmt_bytes
        snap_bits = [f"[provision] 工作区快照：{snap['file_count']} 个文件 "
                     f"{_fmt_bytes(snap['total_bytes'])} 进考场"
                     + (f"；尊重 .gitignore 排除 {len(snap['excluded_dirs'])} 个目录"
                        + (f"（{ '、'.join(d for d, _ in snap['excluded_dirs'][:5]) }…）"
                           if len(snap['excluded_dirs']) > 5 else
                           f"（{'、'.join(d for d, _ in snap['excluded_dirs'])}）")
                        if snap['excluded_dirs'] else "")
                     + (f"；跳过超限单文件 {len(snap['skipped_files'])} 个"
                        if snap['skipped_files'] else "")]
        msg(state["session_id"], "assistant", "".join(snap_bits))
        # 收集闸顺延补跑（2026-11-16 全语言实锤 1178/1204/1138）：单阶段主图 generate
        # 时容器未建（collect_deferred），旧代码只对 two_stage_rebuild 补跑——单阶段
        # 生成物从未过闸 → go build failed 无人拦截 → 0/0 三轮熔断。无条件补跑
        # （生成文件不存在时 collect_gate_files 返回空，零开销）。
        from ..skills.gen_tests import collect_gate_files
        try:
            dropped = collect_gate_files(runner, container, conn, tid, Path(state["workspace"]))
            if dropped:
                msg(state["session_id"], "assistant",
                    "[provision] 顺延的收集闸：废案 " + "、".join(dropped))
        except Exception as e:      # noqa: BLE001 —— 闸失败不引入新失败面（run 轮归因兜底）
            log.warning("顺延收集闸跳过（%s: %s）", type(e).__name__, e)
        dep = registry.invoke(tid, skills["install_deps"], {
            "container_name": state["container_name"],
            "deps_manifest": probe_deps_manifest(state["workspace"],
                                                 state.get("primary_language"))})
        # 构建缓存预热（2026-11-15 卡 4）：首轮把 go/java 依赖树拉进持久缓存（900s
        # fail-open）——二轮起 fresh 重建也命中缓存，冷下载 600s 超时家族根治。
        try:
            warm_msg = warm_build_cache(runner, container,
                                        state.get("primary_language", ""))
            if warm_msg:
                msg(state["session_id"], "assistant", warm_msg)
        except Exception as e:      # noqa: BLE001 —— 预热失败不引入新失败面
            log.warning("构建缓存预热跳过（%s: %s）", type(e).__name__, e)
        # Go 工具链版本闸（2026-11-08 任务 678/679 实锤）：go.mod 要求 go>=1.26 而镜像
        # 只有 1.22 → go test 秒失败零 JSON 事件 → evaluate 0/0 → repair_env 三轮空转熔断。
        # 版本是环境事实（repair 修不动），开考前确定性判定并诚实 stop，不烧修复轮次。
        if state.get("primary_language") == "go":
            try:
                from ..skills.stack_detect import check_go_toolchain
                gv = runner.exec_in(container, ["go", "version"], timeout_s=30)
                gate = check_go_toolchain(state["workspace"], gv.stdout or gv.stderr or "")
                if not gate["ok"]:
                    msg(state["session_id"], "assistant", f"[provision·toolchain] {gate['reason']}")
                    return {"done": True, "verdict": "stop", "stop_reason": gate["reason"]}
            except Exception as e:   # noqa: BLE001 —— 闸自身失败不引入新失败面（fail-open）
                log.warning("Go 工具链闸跳过（%s: %s）", type(e).__name__, e)
        # PHP 平台闸（2026-11-15 卡 2，1148 真机复现实锤：composer.lock 要求 PHP>=8.4.1
        # 而镜像 8.3.36——composer install 装完 vendor 但 phpunit 在 platform_check.php
        # 即 Fatal exit 255，适配器统一文本 0/0 → repair_env 三轮空转熔断）。同 Go 闸
        # 语义；须在 composer install 之后跑（无 vendor 时 check 只查 composer 本体恒过）。
        if state.get("primary_language") == "php" and (Path(state["workspace"]) / "composer.json").exists():
            try:
                pr = runner.exec_in(container,
                                    ["composer", "check-platform-reqs", "--no-interaction"],
                                    timeout_s=60)
                reason = php_platform_gate_result(pr.exit_code, pr.stdout + pr.stderr)
                if reason:
                    msg(state["session_id"], "assistant", f"[provision·toolchain] {reason}")
                    return {"done": True, "verdict": "stop", "stop_reason": reason}
            except Exception as e:   # noqa: BLE001 —— 闸自身失败不引入新失败面（fail-open）
                log.warning("PHP 平台闸跳过（%s: %s）", type(e).__name__, e)
        # L1 服务编排兜底（2026-11-15 存疑 33/34 拍板全修）：repair_env 回炉轮遇连接类
        # 失败（java ConnectException / php curl code=0）→ 从生成用例解析目标端口，
        # 容器内起被测服务（复用 L2 启动器）→ 本轮重跑拿真实响应。fail-open 不炸链路。
        try:
            svc_msg = start_service_for_repair(
                runner, container, Path(state["workspace"]),
                state.get("primary_language", ""), state.get("verdict"),
                (state.get("last_stdout") or "") + "\n" + (state.get("last_stderr") or ""))
            if svc_msg:
                msg(state["session_id"], "assistant", svc_msg)
        except Exception as e:   # noqa: BLE001 —— 起服务失败不引入新失败面
            log.warning("L1 服务编排跳过（%s: %s）", type(e).__name__, e)
        # DB 感知测试环境 v1（用户需求：带库程序测试时库要参与）：检测 DB 依赖 →
        # sqlite 家族自动建库/迁移/种子；服务端库如实降级提示。fail-open 不炸 provision。
        try:
            from ..skills.db_probe import detect_db_usage, prepare_database
            msg(state["session_id"], "assistant", f"[provision·db] 探针执行（工作区 {state['workspace']}）")
            det = detect_db_usage(Path(state["workspace"]))
            if det["family"]:
                msg(state["session_id"], "assistant",
                    f"[provision·db] 检测到数据库依赖：{det['family']}"
                    f"（证据 {len(det['evidence'])} 条，"
                    f"全家族 {det['all_families']}，备库脚本 {det['scripts'] or '无'}，"
                    f"SQL 文件 {det['sql_files'] or '无'}）")
                prep = prepare_database(runner, container, det, Path(state["workspace"]),
                                        state.get("primary_language", ""))
                msg(state["session_id"], "assistant",
                    f"[provision·db] {prep.get('status')}：{prep.get('note', '')[:200]}")
        except Exception as e:  # noqa: BLE001 —— 备库失败如实留提示，不炸主链路
            log.warning("DB 感知预备失败（不影响主链路）: %s: %s", type(e).__name__, e)
        cur = conn.execute(
            """INSERT INTO container_jobs(task_id, module_id, image, status, cpu_limit, mem_limit_mb,
                                          priority, timeout_at, container_id, started_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?, datetime('now', '+' || ? || ' seconds'), ?, datetime('now'))""",
            (tid, state.get("module_id"), image,
             config.DEFAULT_CPU_LIMIT, config.DEFAULT_MEM_LIMIT_MB,
             5, config.DEFAULT_JOB_TIMEOUT_S, container.id))
        conn.commit()
        # 依赖安装失败留痕（2026-11-15 卡 2，1148 实锤：composer exit≠0 只在
        # install_deps 输出里，provision 消息只打"ok"——证据链断在第一环）
        dep_tail = ""
        if dep.status == "ok" and "（失败）" in (dep.output or ""):
            dep_tail = "；⚠ 依赖安装有失败步骤，输出尾部：" + (dep.output or "").strip()[-250:]
        msg(state["session_id"], "assistant",
            f"[provision] 容器 {state['container_name']} 就绪；依赖安装：{dep.status}{dep_tail}")
        return {"job_id": cur.lastrowid}

    # ---------- 4. 生成用例（LLM，未配 Key 时诚实降级；repair_gen 先按策略定向修复） ----------
    def generate(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "generating")
        next_round = state.get("round_no", 0) + 1

        # ②定向修复策略分派（调研报告 §8 行2/行3）：
        # A=prune 剪除+全量重生成（基线）｜B=rewrite 带上下文重写（YATE）｜C=hybrid 合成
        # 共用铁律：断言失败证人永不剪永不重写（§7.2），仓库自带测试不动。
        skip_full_regen = False
        if state.get("round_no", 0) >= 1:
            pairs = [(nid, repair.error_class_of(state.get("last_stdout", ""), nid))
                     for nid in repair.failing_nodeids(state.get("last_stdout", ""))]
            if pairs:
                from ..skills.llm_profile import effective_key
                info = run_repair_strategy(
                    config.REPAIR_STRATEGY, Path(state["workspace"]), pairs, tid, conn,
                    round_no=next_round,
                    # 2026-11-09：统一 key 解析（local 档恒 ollama；原硬编码读 DEEPSEEK_API_KEY
                    # 会让 local profile 下定向修复退化为纯剪除）
                    llm_fn=make_deepseek_fn(effective_key(), task_id=tid),
                    runner=runner, container_name=state["container_name"],
                    requirement=state.get("requirement", ""),
                    fail_summary=(state.get("last_stdout", "") + state.get("last_stderr", ""))[-1500:])
                skip_full_regen = info.get("skip_full_regen", False)
                msg(state["session_id"], "assistant",
                    f"[定向修复·{info.get('strategy', config.REPAIR_STRATEGY)}] "
                    f"剪{len(info.get('pruned', []))} 重写{len(info.get('rewritten', []))} "
                    f"保留证人{len(info.get('kept', []))}：" +
                    json.dumps({k: info[k] for k in ("pruned", "rewritten", "kept", "fallback")
                                if info.get(k)}, ensure_ascii=False)[:400])

        if skip_full_regen:
            msg(state["session_id"], "assistant",
                f"[generate 第{next_round}轮] 坏用例全部重写成功，跳过全量重生成（YATE 路线）")
            return {"round_no": next_round}

        res = registry.invoke(tid, skills["gen_tests"], {
            "workspace": state["workspace"],
            "requirement": state.get("requirement", ""),
            "module_hint": state.get("primary_language", ""),
            "container_name": state["container_name"],
            "module_id": state.get("module_id"),
            "index_scope_paths": state.get("index_scope_paths") or [],
            "endpoint_gaps": state.get("index_endpoint_gaps") or [],
            "agent_diagnosis": state.get("agent_diagnosis"),
            "coverage_gaps": (state.get("per_file") or [])
                             if config.COVERAGE_FEEDBACK_ENABLED else [],  # ①缺口回灌（V4 开关）
            "coverage_target": state.get("coverage_target"),
            "round_no": next_round})
        # skill 自身失败（如 705 实锤：qwen3-4b ctx=8192 装不下 12768 token 提示词）
        # 必须把 error 透传给用户——error 只埋 skill_invocations 台账=归因断点
        if res.status != "ok" or not (res.output or "").strip():
            _fail_detail = (res.error or f"工具返回空（status={res.status}）")[:280]
            msg(state["session_id"], "assistant",
                f"[generate 第{next_round}轮] 生成工具失败：{_fail_detail}"
                f"（详情见 skill_invocations 台账）")
            return {"round_no": next_round,
                    "gen_reason": f"生成工具失败：{_fail_detail}"}
        # B2+（419 教训）：需求-能力错配必须对用户显性可见——独立消息行（不只埋在
        # JSON 截断里），任务详情页/工作台实时日志直接可读
        gen_reason = None
        try:
            _gsum = json.loads(res.output)
            gen_reason = _gsum.get("reason")
            _mm = _gsum.get("capability_mismatch")
            if _mm:
                msg(state["session_id"], "assistant",
                    f"[generate·需求-能力对齐警告] {_mm} 本次生成可能未覆盖你真正想测的功能，"
                    f"请核对需求与产品实际能力。")
            # A3（2026-11-20）：gen_tests 零收录回退全库 → 清空路由锁定——scope 前缀
            # 失配时锁定名存实亡，不清空会让 L2 兜底把"范围内缺席+TOTAL>0"误判 ok
            if _gsum.get("scope_fallback"):
                msg(state["session_id"], "assistant",
                    "[index·scope] 锁定范围产品源码 0 收录（scope 前缀与真实路径失配）"
                    "——本轮已回退全库生成，路由锁定解除（L2 兜底不再介入）")
                return {"round_no": next_round, "gen_reason": gen_reason,
                        "index_scope_paths": [], "index_closure": []}
        except (ValueError, AttributeError):
            pass
        msg(state["session_id"], "assistant", f"[generate 第{next_round}轮] {res.output[:400]}")
        # 空集归因透传（2026-11-09，任务卡-LLM模型选择 B 项）：skill 返回的 reason 随
        # state 上行，prep 收尾的"出题 0 条"提示按实际原因生成，禁止硬编码猜测
        return {"round_no": next_round, "gen_reason": gen_reason}

    # ---------- 5. 执行（容器内 pytest，确认门已在注册表层把住） ----------
    def run(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "running")
        lang = state["primary_language"]
        if lang not in ("python", "javascript", "typescript", "java", "go", "php"):
            # 其余语言：诚实终止，不进归因循环——"链路未实现"不该被伪装成环境失败（A7）
            return {"done": True, "verdict": "stop",
                    "stop_reason": f"{lang} 执行链路 v1 未接入（诚实降级：static_only 路径走静态扫描），任务终止"}
        ws = Path(state["workspace"])
        # 环境闸（归因三闸之一，11.4 恢复计划 §3）：真实用例前金丝雀自检，失败 fail-fast——
        # task_366 教训：环境坏 12 条全错时系统照常产出"疑似产品缺陷"报告。默认关（V 变量惯例）。
        if config.ENV_GATE_ENABLED and state.get("round_no", 1) <= 1:
            from ..skills import attribution
            cres = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                  attribution.canary_cmd(lang), timeout_s=120)
            if not attribution.canary_ok(cres.exit_code, cres.stdout):
                msg(state["session_id"], "assistant",
                    f"[env-gate] 金丝雀未通过（exit={cres.exit_code}）：任务判环境无效，"
                    f"不产出任何产品缺陷结论（fail-fast）")
                return {"done": True, "verdict": "stop",
                        "stop_reason": "环境闸：金丝雀自检未通过（框架/IO），判环境无效——"
                                       "不产出产品缺陷结论（11.4 归因三闸）"}
            msg(state["session_id"], "assistant", "[env-gate] 金丝雀通过：框架/IO 自检 OK")
        if lang in ("javascript", "typescript"):
            # JS/TS 执行链路（Tier2）：jest --json 输出经宿主侧适配器转 pytest 风格文本，
            # 下游 evaluate/repair/report 零改动（docs：js_adapter 模块头）。
            # TS 同路径（白包阶段1）：.test.ts 生成物 + 散装时内联 ts-jest transform 配置
            gen_ext = ".test.ts" if lang == "typescript" else ".test.js"
            gen_files = sorted(p.name for p in ws.glob(f"test_gen_*{gen_ext}"))
            imported = f"test_imported{gen_ext}"
            if (ws / imported).exists():
                gen_files.append(imported)                 # 人工/插入用例（按语言命名）
            cmd = build_run_cmd_js(round_no=state.get("round_no", 1), gen_files=gen_files)
            # 内联配置注入判定收口为纯函数 _js_needs_inline（三档：无配置兜底 853 实锤 /
            # 有配置但限定扫描范围 1291 nestjs 实锤 testMatch 只扫 src/ → 生成物
            # 'No tests found' 0/0 熔断 / 有配置不限定走项目配置零变化）。
            if _js_needs_inline(ws, lang):
                cmd += ["--config", _inline_jest_config(include_ts=(lang == "typescript"))]
                if any((ws / n).exists() for n in
                       ("jest.config.js", "jest.config.ts", "jest.config.mjs",
                        "jest.config.cjs", "jest.config.json")) or _pkg_declares_jest(ws):
                    msg(state["session_id"], "assistant",
                        "[run] 项目 jest 配置限定扫描范围（testMatch/roots/rootDir 子目录），"
                        "工作区根生成物不在扫描范围：注入内联配置执行（1291 nestjs 盲区修复）")
                else:
                    msg(state["session_id"], "assistant",
                        f"[run] 项目无 jest 配置{'/package.json' if not (ws / 'package.json').exists() else ''}："
                        f"注入最小内联配置执行（{'TypeScript（含 ts-jest transform）' if lang == 'typescript' else '散装 JS'} 兜底）")
        elif lang == "java":
            # Java 执行链路（Tier2，白包阶段2）：mvn test → surefire XML/JaCoCo 经 java_adapter
            # 转 pytest 风格文本，下游 evaluate/repair/report 零改动（adapter 统一文本唯一接缝）。
            cmd = build_run_cmd_java(ws)
        elif lang == "go":
            # Go 执行链路（Tier2，白包阶段3）：go test -json -coverprofile → go_adapter
            # 转 pytest 风格文本，下游 evaluate/repair/report 零改动（adapter 统一文本唯一接缝）。
            cmd = build_run_cmd_go()
        elif lang == "php":
            # PHP 执行链路（Tier2，白包阶段4）：phpunit 显式文件清单 → junit XML/clover
            # 经 php_adapter 转 pytest 风格文本，下游零改动（adapter 统一文本唯一接缝）。
            gen_php = sorted(pp.name for pp in ws.glob("TestGen*.php"))
            imported_php = ws / "test_imported.php"
            if imported_php.exists():
                gen_php.append("test_imported.php")
            cmd = build_run_cmd_php(gen_php)
        else:
            # Loop-9：round_no>1 起 cmd 限定生成物+imported——自带 tests 恒定失败不进 R-Loop
            # （三角色实验 30 题实锤：混入后补测永远修不好→熔断/预算耗尽）；
            # 两阶段对话增补文件（test_gen_fb*.py）同属生成物，一并纳入
            gen_files = sorted(p.name for p in ws.glob("test_gen_r*.py"))
            gen_files += sorted(p.name for p in ws.glob("test_gen_fb*.py"))
            if (ws / "test_imported.py").exists():
                gen_files.append("test_imported.py")
            cmd = build_run_cmd(round_no=state.get("round_no", 1), gen_files=gen_files)
        res = registry.invoke(tid, skills["run_tests"],
                              {"container_name": state["container_name"], "cmd": cmd})
        if res.status == "denied":
            raise RuntimeError("run_tests 被确认门拒绝：主流程不应在未确认时到达此节点")
        if res.status == "timeout":
            out = json.dumps({"exit": -1, "stdout": "", "stderr": res.error}, ensure_ascii=False)
        elif res.status != "ok":
            # skill 层失败（如容器瞬态消失/守护进程重启）：转成 env 失败走 repair_env 重建，
            # 而不是 json.loads 空输出把整个任务炸掉（实测踩坑：docker 守护进程中途重启）
            out = json.dumps({"exit": -1, "stdout": "",
                              "stderr": f"run_tests {res.status}: {res.error}"}, ensure_ascii=False)
        else:
            out = res.output
        r = json.loads(out)
        # 任务C：逐用例结果落库（轮次级粒度，每轮跑完即出现——看板数据源）
        try:
            from ..skills.case_results import parse_jest_results, parse_junit_xml, store_case_results
            if lang in ("javascript", "typescript"):
                rows = parse_jest_results(r.get("stdout", ""))   # 转换前抽原始 jest JSON
            elif lang == "java":
                # surefire XML 目录逐文件 cat（容器内无 glob 展开——sh -c 罗列再逐个取），
                # parse_junit_xml 直接复用（surefire 与 pytest junitxml 同 family，S1 调研结论）
                ls = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                    ["sh", "-c",
                                     "ls target/surefire-reports/TEST-*.xml 2>/dev/null"],
                                    timeout_s=15, workdir="/work")
                rows = []
                for xf in ls.stdout.split():
                    jc = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                        ["cat", xf], timeout_s=15, workdir="/work")
                    if jc.ok:
                        rows.extend(parse_junit_xml(jc.stdout))
            elif lang == "go":
                # go test -json 事件流逐用例落库（白包阶段3；转换前抽原始事件，同 jest 口径）
                from ..skills.go_adapter import go_test_json_to_rows
                rows = go_test_json_to_rows(r.get("stdout", ""),
                                            r.get("stderr", ""))   # 658 实锤：编译错误在 stderr
            elif lang == "php":
                # phpunit junit XML 与 surefire 同 family（白包阶段4），parse_junit_xml 直接复用
                jc = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                    ["cat", "/tmp/php_junit.xml"], timeout_s=15, workdir="/work")
                rows = parse_junit_xml(jc.stdout) if jc.ok else []
            else:
                jc = runner.exec_in(
                    runner.client.containers.get(state["container_name"]),
                    ["cat", config.JUNIT_PATH], timeout_s=30)
                rows = parse_junit_xml(jc.stdout) if jc.ok else []
            n_stored = store_case_results(conn, tid, state.get("round_no", 1), rows) if rows else 0
            if n_stored:
                msg(state["session_id"], "assistant",
                    f"[run 第{state.get('round_no', 1)}轮] 逐用例结果 {n_stored} 条落库"
                    f"（通过 {sum(1 for x in rows if x['status'] == 'passed')}"
                    f" / 失败 {sum(1 for x in rows if x['status'] == 'failed')}）")
        except Exception as e:              # noqa: BLE001 —— 看板数据缺失不炸执行主链路
            log.warning("逐用例结果落库失败 task=%s: %s", tid, e)
        if lang in ("javascript", "typescript") and r.get("stdout"):
            from ..skills.js_adapter import jest_json_to_pytest_text
            r["stdout"] = jest_json_to_pytest_text(r["stdout"], r.get("stderr", ""))
        if lang == "java":
            # java_adapter：surefire XML+JaCoCo → 统一文本（S5 唯一合法接缝，下游零改动）
            from ..skills.java_adapter import surefire_dir_to_pytest_text
            rep = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                 ["sh", "-c", "mkdir -p /tmp/sf && "
                                  "cp target/surefire-reports/TEST-*.xml /tmp/sf/ 2>/dev/null; "
                                  "ls /tmp/sf/ >/dev/null 2>&1 && echo ok"],
                                 timeout_s=20, workdir="/work")
            if rep.ok and "ok" in rep.stdout:
                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    for xf in runner.exec_in(runner.client.containers.get(state["container_name"]),
                                             ["sh", "-c", "ls /tmp/sf/"], timeout_s=10,
                                             workdir="/").stdout.split():
                        xj = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                            ["cat", f"/tmp/sf/{xf}"], timeout_s=15, workdir="/")
                        if xj.ok:
                            (Path(td) / xf).write_text(xj.stdout, encoding="utf-8")
                    jacoco = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                            ["sh", "-c",
                                             "cat target/site/jacoco/jacoco.xml 2>/dev/null || true"],
                                            timeout_s=15, workdir="/work")
                    jacoco_path = None
                    if jacoco.ok and (jacoco.stdout or "").strip():
                        jacoco_path = Path(td) / "jacoco.xml"
                        jacoco_path.write_text(jacoco.stdout, encoding="utf-8")
                    r["stdout"] = surefire_dir_to_pytest_text(td, jacoco_xml=jacoco_path)
                    # 任务6 java 半（2026-11-22）：多模块兜底——旗标开时 find /work 全部
                    # jacoco.xml（限定 /work 别扫容器系统目录——交接文档坑段），≥2 份
                    # 或根 target 无而模块有 → jacoco_multimodule 合并渲染统一 TOTAL。
                    # 关=本块零执行，现状逐字节不变。
                    if config.JACOCO_MULTIMODULE_FALLBACK:
                        fnd = runner.exec_in(
                            runner.client.containers.get(state["container_name"]),
                            ["sh", "-c", "find /work -name jacoco.xml 2>/dev/null"],
                            timeout_s=15, workdir="/")
                        found = [l for l in (fnd.stdout or "").splitlines() if l.strip()]
                        need = (len(found) >= 2
                                or (found and not jacoco_path))
                        if need:
                            texts = []
                            for fp in found[:12]:   # 封顶防极端
                                xr = runner.exec_in(
                                    runner.client.containers.get(state["container_name"]),
                                    ["cat", fp], timeout_s=15, workdir="/")
                                if xr.ok and xr.stdout.strip():
                                    texts.append(xr.stdout)
                            from ..skills.java_adapter import render_multimodule_coverage
                            seg = render_multimodule_coverage(texts)
                            if seg:
                                r["stdout"] = (r.get("stdout") or "") + "\n" + seg
            else:
                r["stdout"] = (r.get("stdout") or "") + "\n(no surefire reports: 编译失败或用例未执行)\n"
        if lang == "go":
            # go_adapter：go test -json 事件流+coverprofile → 统一文本（S5 唯一合法接缝，下游零改动）
            from ..skills.go_adapter import go_test_json_to_pytest_text
            cov = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                 ["cat", "/tmp/go_cover.out"], timeout_s=15, workdir="/")
            r["stdout"] = go_test_json_to_pytest_text(
                r.get("stdout", ""), cov.stdout if (cov.ok and cov.stdout.strip()) else None,
                r.get("stderr", ""))   # 658 实锤：编译错误在 stderr，进 message 保归因链
        if lang == "php":
            # php_adapter：junit XML+clover(pcov) → 统一文本（S5 唯一合法接缝，下游零改动）。
            # junit 必须从容器 cat /tmp/php_junit.xml——原始 stdout 是 phpunit 人读文本，
            # 喂进解析器=全 0 计数（真机实锤：首轮 smoke 0/0 三轮熔断即此坑）
            from ..skills.php_adapter import phpunit_junit_to_pytest_text
            jc = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                ["cat", "/tmp/php_junit.xml"], timeout_s=15, workdir="/work")
            cv = runner.exec_in(runner.client.containers.get(state["container_name"]),
                                ["cat", "/tmp/php_clover.xml"], timeout_s=15, workdir="/work")
            has_junit = bool(jc.ok and jc.stdout.strip())
            r["stdout"] = php_unified_with_raw(
                phpunit_junit_to_pytest_text(
                    jc.stdout if has_junit else "",
                    cv.stdout if (cv.ok and cv.stdout.strip()) else None),
                r.get("stdout") or "", r.get("stderr") or "", has_junit)
        return {"last_exit": r["exit"], "last_stdout": r["stdout"], "last_stderr": r["stderr"],
                "last_cmd": cmd}

    # ---------- 6. 判读（Evaluator 在宿主侧：解析 + 归因 + R-Loop 判定） ----------
    def evaluate(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "reporting")
        stdout, stderr = state.get("last_stdout", ""), state.get("last_stderr", "")
        passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", stdout)) else 0
        failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", stdout)) else 0
        skipped = int(m.group(1)) if (m := re.search(r"(\d+) skipped", stdout)) else 0
        errors = int(m.group(1)) if (m := re.search(r"(\d+) errors?", stdout)) else 0
        failed += errors
        coverage = None
        if m := re.search(r"TOTAL.*?\s(\d+)%", stdout):
            coverage = int(m.group(1)) / 100

        # V8 证人冻结+继续补测：断言失败用例累积为冻结证人（当轮产生当轮生效），
        # 证人的失败指纹不参与熔断——有意保留≠原地打转；开关关时下面三处与
        # 既有口径逐字节一致（witness 恒空、fp 不排除、decide 不传证人）。
        witness = list(state.get("witness_nodeids") or [])
        if config.WITNESS_CONTINUE_ENABLED:
            for nid in repair.assertion_failure_nodeids(stdout):
                if nid not in witness:
                    witness.append(nid)
        fp = repair.fingerprint_of(
            stdout, stderr,
            exclude_nodeids=tuple(witness) if config.WITNESS_CONTINUE_ENABLED else ())
        # PHP 退出码语义归一（2026-11-15 卡 2，1149 实锤：17 过 0 败 exit≠0 →
        # classify 兜底 env → repair_env 三轮熔断——phpunit warnings/risky 也非零退出）
        exit_code = php_exit_for_decide(state.get("primary_language", "python"),
                                        state.get("last_exit", 1), passed, failed)
        if exit_code != state.get("last_exit", 1):
            msg(state["session_id"], "assistant",
                f"[run·exit 语义] phpunit exit={state.get('last_exit')} 但 {passed} 过 "
                f"{failed} 败——按失败计数判别（phpunit warnings/risky 非零退出不计失败），按通过处理")
        verdict, kind, reason = repair.decide(
            exit_code, stdout, stderr,
            state.get("fingerprints", []), fp,
            state.get("round_no", 0), coverage,
            state.get("coverage_target"),
            witness_nodeids=witness if config.WITNESS_CONTINUE_ENABLED else None)
        # API 型黑盒覆盖率不可达声明（2026-11-16 存疑 35② 拍板"去修复吧"落地）：
        # js/php 生成用例走真 HTTP（黑盒），被测服务在另一进程——istanbul/clover
        # 插桩结构性测不到（1161 恒 2%/1208 N/A 实锤），"通过但覆盖率低→补测"永远
        # 补不出来 → 三轮同指纹熔断收尾更差。识别=生成用例含 HTTP 客户端特征；
        # 全过（0 败）且 decide 给 repair_gen（覆盖率卡住）→ 诚实 stop 声明
        # （接口通过数为准）。有失败用例时不适用（正常归因分诊）。
        bb_blackbox = detect_api_blackbox(Path(state.get("workspace", "")),
                                          state.get("primary_language", ""))
        bb_endpoint_cov = None
        if bb_blackbox and verdict == "repair_gen" and failed == 0 and exit_code == 0:
            # 端点对账（2026-11-19 复盘：出口宣称"接口级为准"必须核实）——
            # 注册端点取 interface_endpoints（L2 路由提取），空=无从对账按旧行为
            reg_rows = conn.execute(
                "SELECT method, path FROM interface_endpoints WHERE task_id=?", (tid,)).fetchall()
            if reg_rows:
                reg_set = {f"{r['method'].upper()} {_norm_ep_path(r['path'])}" for r in reg_rows}
                called = _endpoint_calls_in_tests(Path(state.get("workspace", "")),
                                                  state.get("primary_language", ""))
                missed = sorted(reg_set - called)
                bb_endpoint_cov = {"covered": len(reg_set) - len(missed),
                                   "total": len(reg_set), "missed": missed}
        bb = api_blackbox_stop(
            bb_blackbox, verdict, failed, exit_code, passed, coverage, bb_endpoint_cov)
        if bb:
            verdict, kind, reason = bb
        elif (bb_blackbox and bb_endpoint_cov and bb_endpoint_cov.get("missed")
              and failed == 0 and exit_code == 0):
            state["index_endpoint_gaps"] = list(bb_endpoint_cov["missed"])
            msg(state["session_id"], "assistant",
                "[api-blackbox] 端点对账 "
                f"{bb_endpoint_cov['covered']}/{bb_endpoint_cov['total']} 已测，"
                f"缺口 {bb_endpoint_cov['missed'][:4]}"
                f"{'…' if len(bb_endpoint_cov['missed']) > 4 else ''}——"
                "缺口注入补测提示词（轮次预算兜底）")
        # —— 归因三闸·差分闸（11.4 恢复计划 §3；LIBRO buggy/fixed 口径，默认关）——
        # 疑似产品缺陷先在对照 ref 复跑失败子集：对照也失败 → 排除产品缺陷（用例/环境错），
        # 对照通过 → 高置信缺陷暴露。不可用（no_git/no_ref）→ 维持原归因，不硬猜。
        if (config.DIFF_GATE_ENABLED and kind == "product_defect"
                and state.get("last_exit", 1) != 0):
            from ..skills import attribution
            lang = state.get("primary_language", "python")
            failed_files = sorted({nid.split("::")[0]
                                   for nid in repair.failing_nodeids(stdout)})
            if failed_files:
                def _exec(cmd, timeout_s=600):
                    return runner.exec_in(
                        runner.client.containers.get(state["container_name"]),
                        cmd, timeout_s=timeout_s)
                diff = attribution.run_diff_gate(
                    _exec, lang, failed_files,
                    state.get("diff_ref") or config.DIFF_GATE_REF,
                    bug_stderr=state.get("last_stderr", ""))
                if diff["label"] == "case_defect":
                    kind = "case_defect"
                    # detail 自带「对照 <ref> 上…」全句，这里不再重复拼接（真机验证实锤）
                    reason = (f"差分闸降级（{diff['confidence']}置信）："
                              f"{diff['detail']} → 非产品缺陷（用例/环境错）")
                msg(state["session_id"], "assistant",
                    f"[diff-gate] status={diff['status']} {diff['detail']}"
                    f"{('；原归因 product_defect 已降级为 ' + kind) if kind != 'product_defect' else ''}"
                    if diff["status"] == "ok" else
                    f"[diff-gate] 不可用（{diff['status']}）：{diff['detail']}，维持原归因")
                if diff["status"] == "restore_failed":
                    msg(state["session_id"], "assistant",
                        "[diff-gate] ⚠ 工作区未切回原 HEAD——必须人工检查 git 状态后再用该工作区")
        # —— 归因三闸·flaky 连跑（TestGen-LLM 口径，默认 FLAKY_RERUNS=0 关）——
        # 疑似产品缺陷终判前同命令连跑 N 次：结果不一致 → stop_reason 标 flaky，
        # 报告侧据此把该轮不计入缺陷分母（taxonomy 不新增值，v1 靠 reason 携带）。
        if (config.FLAKY_RERUNS > 0 and kind == "product_defect"
                and state.get("last_exit", 1) != 0 and state.get("last_cmd")):
            from ..skills import attribution
            lang = state.get("primary_language", "python")
            outcomes = []
            for _ in range(int(config.FLAKY_RERUNS)):
                rr = runner.exec_in(
                    runner.client.containers.get(state["container_name"]),
                    state["last_cmd"], timeout_s=config.DEFAULT_JOB_TIMEOUT_S)
                outcomes.append(attribution.outcome_passed(
                    lang, rr.exit_code, rr.stdout))
            if outcomes and attribution.is_flaky(outcomes):
                reason += (f"；flaky 连跑 {config.FLAKY_RERUNS} 次结果不一致"
                           f"（{sum(outcomes)} 过/{len(outcomes) - sum(outcomes)} 败）"
                           "→ 证据强度降级，建议人工复跑定性")
        # —— L2 覆盖率闭环兜底（2026-11-18 设计方案 §2；急救层，仅路由格触发） ——
        # 跑后校验"锁定范围有没有真覆盖"：零覆盖=路由标签漏了→扩圈重跑一次；仍零
        # 覆盖=放弃路由回退全库（急救失败的诚实出口，CODAMOSA 思想）。只在 repair_gen
        # 分支触发——指纹熔断/轮次预算给出的 stop 绝不覆盖（急救不得击穿熔断，
        # 设计方案 §2.3 L2-7 成本护栏：至多扩圈一次+回退一次，轮数不超 MAX_REPAIR_ROUNDS）。
        per_file_rows = _parse_per_file_coverage(stdout)
        if (config.INDEX_L2_RESCUE_ENABLED and verdict == "repair_gen"
                and state.get("index_scope_paths")):
            try:
                from ..skills.project_index import coverage_check
                chk = coverage_check(state, per_file_rows, coverage_line=coverage)
                if chk["verdict"] == "zero_coverage":
                    if state.get("index_l2_rescued"):
                        state["index_scope_paths"] = []
                        verdict = "repair_gen"
                        reason = "L2 急救失败：扩圈后仍零覆盖——放弃路由回退全库重跑"
                        msg(state["session_id"], "assistant",
                            "[index·L2] 扩圈后仍零覆盖——放弃路由回退全库（急救失败诚实出口）")
                    else:
                        closure2 = None
                        origin = (state.get("index_closure") or [""])[0]
                        if origin:
                            try:
                                from ..skills.project_index import (dependency_closure,
                                                                    ensure_index,
                                                                    scope_prefixes_for)
                                idx2 = ensure_index(tid, Path(state["workspace"]),
                                                    [state.get("primary_language", "")
                                                     or "python"])
                                cand = dependency_closure(idx2, origin, hops=2,
                                                          callgraph=idx2.get("callgraph"))
                                if len(cand) > len(state["index_closure"]):
                                    closure2 = cand
                            except Exception:  # noqa: BLE001 —— 扩圈失败=直接回退全库
                                closure2 = None
                        state["index_l2_rescued"] = True
                        verdict = "repair_gen"
                        if closure2:
                            old_closure = state.get("index_closure") or [origin]
                            added = len(closure2) - len(old_closure)
                            state["index_closure"] = closure2
                            # A3（2026-11-20）：扩圈 scope 同样用索引真实路径（修复前拼模块名）
                            state["index_scope_paths"] = scope_prefixes_for(idx2, closure2)
                            reason = (f"L2 急救：锁定模块零覆盖——扩圈 2 跳重跑"
                                      f"（新增 {added} 邻居模块，闭包 {len(closure2)} 模块）")
                            msg(state["session_id"], "assistant",
                                f"[index·L2] 锁定模块零覆盖——扩圈重跑一次"
                                f"（新增 {added} 邻居模块并入 scope，闭包共 {len(closure2)} 模块）")
                        else:
                            state["index_scope_paths"] = []
                            reason = "L2 急救：零覆盖且扩圈无新增——回退全库重跑"
                            msg(state["session_id"], "assistant",
                                "[index·L2] 锁定模块零覆盖且扩圈无新增——回退全库重跑（急救）")
            except Exception as e:  # noqa: BLE001 —— 急救校验失败不阻断评估主链路
                msg(state["session_id"], "assistant",
                    f"[index·L2] 覆盖率急救校验失败（{type(e).__name__}: {str(e)[:80]}），主链路继续")
        round_no = state.get("round_no", 0)
        # P1-③：每轮评估结构化落库（evaluation_rounds，轨迹唯一真源）——此前只写
        # 叙述消息，collect_metrics 靠正则解析散文取轨迹，措辞一变即静默清空。
        # 重跑再进图 round 从 1 重新计：delete-first 以本轮为准（与 plan 同幂等语义）
        if round_no <= 1:
            conn.execute("DELETE FROM evaluation_rounds WHERE task_id=? AND level='L1'",
                         (tid,))   # 级别过滤：L1 重跑不得清掉 L2 leg 的轮次行
        conn.execute(
            """INSERT INTO evaluation_rounds(task_id, round_no, passed, failed, skipped,
                                            coverage_line, verdict, failure_kind,
                                            stop_reason, fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, round_no, passed, failed, skipped, coverage, verdict, kind, reason, fp))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[evaluate 第{round_no}轮] 通过{passed} 失败{failed} 跳过{skipped} "
            f"覆盖率{coverage if coverage is not None else 'N/A'} 指纹{fp[:8] or '无'} → {verdict}（{reason}）")
        # 0/0 轮根因留痕（853刷题系统实锤：jest 输出解析不到时 0/0/N/A，用户只看到
        # "环境类失败"却不知道 jest 到底报了什么）——把原始输出尾部写进日志
        if passed == 0 and failed == 0:
            tail = (stdout or stderr or "")[-400:].strip()
            msg(state["session_id"], "assistant",
                "[evaluate·raw] 0 用例 0 失败（解析不到测试计数），执行输出尾部：" + (tail or "（空输出）"))
        # L1 raw·stderr 补全（2026-11-15 卡 2，1149 证据蒸发家族）：exit≠0 且 stderr
        # 有内容时 stderr 尾部同样落日志——此前 0/0 分支只打 stdout 优先，stderr 里
        # 的 fatal/连接拒绝证据可能整轮不可见（L2 已有 raw 通道，L1 缺）
        if state.get("last_exit", 0) != 0 and (stderr or "").strip():
            msg(state["session_id"], "assistant",
                "[evaluate·raw·stderr] exit≠0，执行 stderr 尾部：" + stderr[-400:].strip())
        # per-case 失败明细进日志（/defects API 与归因面板的数据源——此前从未写入，
        # F18-c 的 FAILED 行提取一直为空；Python/JS 同口径：nodeid - 错误类: 失败消息摘要）
        for nid in repair.failing_nodeids(stdout)[:20]:
            msg(state["session_id"], "assistant",
                f"[evaluate·failed] FAILED {nid} - "
                f"{repair.failure_summary_of(stdout, nid) or repair.error_class_of(stdout, nid) or 'Error'}")
        return {"passed": passed, "failed": failed, "skipped": skipped,
                "coverage_line": coverage, "per_file": per_file_rows,
                "fingerprint": fp, "failure_kind": kind,
                "verdict": verdict, "stop_reason": reason,
                "witness_nodeids": witness,
                # L2 急救改过的路由键必须走返回值跨节点（366 教训：只改 state 不返回=丢弃）
                "index_scope_paths": state.get("index_scope_paths") or [],
                "index_closure": state.get("index_closure") or [],
                "index_l2_rescued": bool(state.get("index_l2_rescued")),
                "index_endpoint_gaps": state.get("index_endpoint_gaps") or [],
                "last_stdout": stdout,   # 408 实锤：验证轮 env_blocked 分类需要诊断段，不回传即证据蒸发
                "fingerprints": state.get("fingerprints", []) + [fp]}

    # ---------- 7. 报告落库 + 诚实复盘 ----------
    def report(state: TaskState) -> dict:
        tid = state["task_id"]
        ok = state.get("verdict") == "pass"
        conn.execute(
            """INSERT INTO test_reports(task_id, module_id, passed, failed, skipped,
                                        coverage_line, defects, per_file, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, state.get("module_id"), state.get("passed", 0), state.get("failed", 0),
             state.get("skipped", 0), state.get("coverage_line"),
             json.dumps({"failure_kind": state.get("failure_kind"),
                         "stop_reason": state.get("stop_reason")}, ensure_ascii=False),
             json.dumps(state.get("per_file") or [], ensure_ascii=False), None))
        final = "done" if ok else "failed"
        conn.execute("UPDATE test_tasks SET status=?, error=?, updated_at=datetime('now') WHERE id=?",
                     (final, None if ok else state.get("stop_reason"), tid))
        conn.execute("UPDATE container_jobs SET status=?, finished_at=datetime('now') WHERE id=?",
                     ("done" if ok else "failed", state.get("job_id", 0)))
        conn.execute("UPDATE agent_sessions SET ended_at=datetime('now') WHERE id=?",
                     (state["session_id"],))
        conn.commit()
        digest = (f"[digest] 终态 {final}｜通过{state.get('passed', 0)} 失败{state.get('failed', 0)}"
                  f"｜覆盖率{state.get('coverage_line')}｜{state.get('stop_reason')}")
        msg(state["session_id"], "assistant", digest)
        return {"done": True, "stop_reason": state.get("stop_reason")}

    # ---------- 2.5 测试计划（RTM：需求 → 测试条件，测试分析阶段的产物） ----------
    def plan(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "analyzing")  # 测试条件制定属测试分析阶段（理论章口径）
        requirement = (state.get("requirement") or "").strip()
        from ..skills.llm_profile import current, effective_key, missing_key_message
        api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析（任务卡-LLM模型选择）
        if not config.PLAN_ENABLED:   # 实验变量 V3：plan 对照组
            msg(state["session_id"], "assistant", "[plan] 实验开关关闭（对照组）：跳过测试条件拆解")
            return {}
        if not requirement or not api_key:
            why = ("无需求文本" if not requirement
                   else missing_key_message(current()))
            msg(state["session_id"], "assistant", f"[plan] {why}：跳过测试条件拆解（诚实降级）")
            return {}
        # B5：带退避重试的同签名构造——必须先于 understand 回调赋值：回调读 llm 的
        # _ctx_budget/_base_url 记账元数据，晚赋值=回调触发 NameError 被吞、
        # 理解层调用零记账（2026-11-15 闲时找bug 第一轮实锤）
        from ..skills.llm_client import make_chat
        llm = make_chat(phase="plan", api_key=api_key, temperature=0.1,
                        timeout=60, max_retries=2)
        # 需求理解与提炼层（UNDERSTAND_ENABLED 默认关）：任意形态输入（一句话/小白话/任务书）
        # → 测试方向/目标/显式假设。产物是策略不是条件——条件仍必须锚真值清单（闸不放松）；
        # 产物经 agent_messages 留痕（可审计），fail-open：理解失败透传原文。
        understanding = None
        if config.UNDERSTAND_ENABLED:
            from ..skills.understand import understand_requirement
            understanding = understand_requirement(
                api_key, requirement,
                project_hint=str(state.get("primary_language") or ""),
                on_resp=lambda resp: log_llm_call(tid, "understand", resp,
                    ctx_budget=getattr(llm, "_ctx_budget", None),
                    base_url=getattr(llm, "_base_url", "")))
            if understanding:
                goals = "；".join(understanding["test_goals"][:3])
                extra = (f"（显式假设 {len(understanding['assumptions'])} 条）"
                         if understanding["assumptions"] else "")
                msg(state["session_id"], "assistant",
                    f"[understand] 需求理解提炼：方向 {goals}{extra}")
            else:
                msg(state["session_id"], "assistant",
                    "[understand] 理解层未产出（fail-open 透传原文拆解）")
        # BREADTH 广度采样（默认关=source_modules=None，提示词逐字节不变）：
        # plan 注入被测源码结构清单+广度纪律——治「条件全挤核心函数域」（E9 task432 实锤）
        src_outline = None
        if config.BREADTH_SAMPLING_ENABLED:
            from ..skills.gen_tests import source_outline
            ws = state.get("workspace") or (config.DATA_DIR / "workspaces" / f"task_{tid}")
            src_outline = (source_outline(Path(ws),
                                          (state.get("module_hint") or "python").lower())
                           or {}).get("files")
            if src_outline:
                msg(state["session_id"], "assistant",
                    f"[plan] 广度采样：源码结构清单 {len(src_outline)} 项注入拆解提示词")
        resp = llm.invoke([("system", PLAN_SYSTEM),
                           ("user", build_plan_prompt(requirement,
                                                      hotspots=state.get("complexity_hotspots"),
                                                      source_modules=src_outline,
                                                      understanding=understanding,
                                                      agent_strategy=state.get("agent_strategy")))])
        log_llm_call(tid, "plan", resp,
                     ctx_budget=getattr(llm, "_ctx_budget", None),
                     base_url=getattr(llm, "_base_url", ""))
        conds = parse_conditions(resp.content)
        if not conds:
            msg(state["session_id"], "assistant", "[plan] 需求拆解不可解析，跳过（诚实降级）")
            return {}
        # delete-first：任务重跑会再次进图，裸 INSERT 会让条件翻倍（P1-①）；
        # 以本轮拆解为准整体替换，与 analyze 的 upsert 同为重跑幂等语义
        conn.execute("DELETE FROM test_conditions WHERE task_id=?", (tid,))
        for c in conds:
            conn.execute(
                """INSERT INTO test_conditions(task_id, requirement_ref, condition, priority, method_hint)
                   VALUES (?,?,?,?,?)""",
                (tid, c.get("requirement_ref"), c["condition"],
                 c.get("priority"), c.get("method_hint")))
        conn.commit()
        msg(state["session_id"], "assistant",
            f"[plan] 需求拆解为 {len(conds)} 条测试条件（RTM 落库）：" +
            "; ".join(f"[{c.get('priority') or '中'}]{c['condition'][:36]}"
                      for c in conds)[:400])
        return {}

    # ---------- 2.6 静态模式兜底（Tier3，2026-11-01）：扫描 + AI 设计草稿，不执行 ----------
    def static_draft(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "generating")   # 复用"生成"语义：生成的是设计草稿非可执行用例
        ws = Path(state["workspace"])
        # 多测试路线编排（期 A，MTA_ROUTER 默认关）：static_only 但路由器找到可执行
        # 路线（如纯 HTML 的 UI 交互幂等）→ 走真实执行出口而非仅静态草稿
        ui_routes = state.get("ui_routes") or []
        if "ui_interaction_idempotency" in ui_routes:
            set_status(conn, tid, "generating")
            from ..skills.ui_idempotency import run_idempotency
            from ..skills.llm_profile import effective_key
            res = registry.invoke(tid, skills["ui_idempotency"], {
                "workspace": str(ws), "api_key": effective_key()})
            try:
                _d = json.loads(res.output)
            except (ValueError, AttributeError):
                _d = {"reason": (res.error or "")[:200]}
            if _d.get("executed"):
                summary = {"drafts": 0, "executed": _d["executed"],
                           "passed": _d.get("passed", 0), "failed": _d.get("failed", 0)}
                # 行级落库（统一 schema 最后一环）：origin 由 store 按 nodeid 匹配生成用例名
                if _d.get("rows"):
                    from ..skills.case_results import store_case_results
                    store_case_results(conn, tid, 1, _d["rows"])
                msg(state["session_id"], "assistant",
                    f"[router·ui-idempotency] 交互幂等用例真跑：{_d['executed']} 条 "
                    f"（{_d.get('passed', 0)} 过/{_d.get('failed', 0)} 败）——"
                    f"oracle=状态不变量，失败行已进 evaluate 契约")
                conn.commit()
                return {"done": True, "verdict": "pass" if not _d.get("failed") else "stop",
                        "failure_kind": "product_defect" if _d.get("failed") else "static",
                        "stop_reason": ("UI 交互幂等路线：发现重复提交缺陷（状态不变量被打破），"
                                        "如实上报" if _d.get("failed") else
                                        "UI 交互幂等路线：交互通过幂等不变量"),
                        "static_summary": summary,
                        "last_stdout": _d.get("summary_text", "")}
            msg(state["session_id"], "assistant",
                f"[router·ui-idempotency] 交互幂等路线未产出：{_d.get('reason', '')[:150]}"
                f"→ 回退静态草稿（诚实降级）")
        from ..skills.llm_profile import effective_key
        api_key = effective_key()   # 显式无→按当前 profile 的 key_env 解析
        summary: dict
        if api_key:
            from ..skills.static_scan import generate_static_drafts
            out = generate_static_drafts(api_key, tid, ws, state.get("requirement", ""), conn)
            summary = json.loads(out)
            msg(state["session_id"], "assistant",
                f"[static-draft] 扫描+设计草稿：{summary.get('drafts', 0)} 条草稿落库"
                f"（framework=static_draft），扫描 {summary.get('scan', {}).get('files', '?')} 文件/"
                f"{summary.get('scan', {}).get('risk_count', '?')} 风险线索"
                if summary.get("drafts") else f"[static-draft] {summary.get('reason', out[:200])}")
        else:
            registry.invoke(tid, skills["static_scan"], {"workspace": str(ws)})
            summary = {"drafts": 0}
            msg(state["session_id"], "assistant",
                "[static-draft] 无 API Key：仅完成确定性扫描（详见审计台账），草稿跳过（诚实降级）")
        conn.commit()
        return {"done": True, "verdict": "pass",
                "failure_kind": "static",
                "stop_reason": ("静态模式（Tier3）：确定性扫描+测试设计草稿已落库，未在容器执行——"
                                "无覆盖率/R-Loop 数据，报告明示未验证"),
                "static_summary": summary}

    # ---------- 8. 评估阶段：五工具编排 + 报告二次审查（vision §1.6 双闸回炉） ----------
    def review(state: TaskState) -> dict:
        tid = state["task_id"]
        set_status(conn, tid, "reporting")   # 评估属报告阶段；状态机 CHECK 枚举不扩（老库无 CHECK 迁移）
        verdict = state.get("verdict", "")
        ok = verdict == "pass"
        try:
            result = _review_body(state, tid, verdict)
        finally:
            # 终态恢复：report 节点已写 done/failed，review 入口的 reporting 只是过程态；
            # 无论审查 pass/reject，任务终态跟随执行判定（审查结论落 test_reports.review，不改判任务）
            set_status(conn, tid, "done" if ok else "failed")
        # 缺陷交接自动落盘（用户 2026-11-04 硬要求：每个任务到最后一步必须生成）；
        # 失败只留痕不上抛——交付物生成问题不允许改判任务终态（诚实边界）
        session = state.get("session_id")
        try:
            auto_export_handoff(conn, tid,
                                notify=(lambda s: msg(session, "assistant", s)) if session else None)
        except Exception as e:
            log.warning("缺陷交接自动落盘失败（不影响任务终态）: %s", e)
        return result

    def _review_body(state: TaskState, tid: int, verdict: str) -> dict:
        # 快照一致性：指标包只 collect 一次，gen_report / review_report 经 args 共用同一份——
        # 各自现 collect 会让"评估工具自身台账"在两次采集间漂移，LLM 闸永远可能自审自 reject
        res_m = registry.invoke(tid, skills["collect_metrics"], {})
        if res_m.status != "ok" or "no_report" in (res_m.output or "{}"):
            msg(state["session_id"], "assistant",
                "[review] 无报告数据（static_only/诚实短路链路），跳过评估五工具")
            return {}
        metrics = json.loads(res_m.output)

        res_r = registry.invoke(tid, skills["map_requirements"], {"use_llm": True})
        rtm = json.loads(res_r.output) if res_r.status == "ok" else {"matrix": []}
        registry.invoke(tid, skills["gen_report"], {"metrics": metrics, "rtm": rtm})
        msg(state["session_id"], "assistant",
            f"[review·collect] 通过{metrics['passed']} 失败{metrics['failed']}"
            f" 覆盖率{metrics['coverage_line']}｜工具成功率{metrics.get('tool_success_rate')}"
            f"｜六要素完整率{metrics.get('six_elem_rate')}")
        # 混合模型构成标注（2026-11-10 诚实纪律）：llm_calls 确定性聚合，不让 LLM 编；
        # 单模型时不打扰（只有真的混了才显示）
        try:
            _mrows = conn.execute(
                "SELECT model, COUNT(*) n FROM llm_calls WHERE task_id=? GROUP BY model",
                (tid,)).fetchall()
            if len(_mrows) > 1:
                _mix = "、".join(f"{r['model']}×{r['n']}" for r in _mrows)
                msg(state["session_id"], "assistant",
                    f"[review·models] 本任务混合模型构成：{_mix}"
                    f"（按 phase 路由；实验数据口径以 llm_usage 台账为准）")
        except Exception as e:      # noqa: BLE001 —— 标注失败不影响审查主链路
            log.warning("模型构成标注失败: %s", e)
        if res_r.status == "ok":
            msg(state["session_id"], "assistant",
                f"[review·rtm] 条件覆盖 {rtm['covered']}/{rtm['conditions_total']}"
                f"（缺口 {rtm['gap']}，映射方式 {rtm['source']}）"
                + (f"｜{rtm['note']}" if rtm.get("note") else ""))

        res_v = registry.invoke(tid, skills["review_report"],
                                {"metrics": metrics, "verdict": verdict})
        if res_v.status != "ok":
            msg(state["session_id"], "assistant",
                f"[review] 审查工具失败（{res_v.error[:120]}）：fail-open 放行，留痕备查")
            return {}
        rev = json.loads(res_v.output)
        msg(state["session_id"], "assistant",
            f"[review·审查] {rev['status']}（{rev['reviewer']}）"
            + ("；原因：" + "; ".join(rev["reasons"])[:300] if rev["reasons"] else ""))

        paths = []
        if res_r.status == "ok":
            # 单快照语义（run46 根修收官）：charts 也吃同一份 metrics/rtm 下传——
            # 叙述、审查、图表三类产物全部派生自 collect 一次的同一时点，彼此零漂移
            res_c = registry.invoke(tid, skills["gen_charts"], {"metrics": metrics, "rtm": rtm})
            if res_c.status == "ok":
                paths = json.loads(res_c.output).get("charts", [])
                if paths:
                    msg(state["session_id"], "assistant",
                        f"[review·charts] {len(paths)} 张图 → data/reports/charts/")
            else:
                # fail-open 放行但要留痕（对照 review_report 失败留痕纪律，
                # 2026-11-15 闲时找bug：旧实现静默吞，图表区无声消失无从排查）
                msg(state["session_id"], "assistant",
                    f"[review·charts] 图表生成失败（fail-open 放行，留痕备查）："
                    f"{getattr(res_c, 'error', '')[:160]}")

        # 回炉语义（vision §1.6）：stop（product_defect）是证人冻结的上报，审查只修叙述不改判。
        # 受控重写（快照单点根修后解锁）：reject → 以新快照重写报告一次 → 复审一次，
        # 无论复审结果如何一律收敛（硬上限 1 次、在节点内完成、不回测试环——run46 的
        # 14 轮死循环由构造排除：重写只改叙述，不改数据）。
        if rev["status"] == "reject":
            if state.get("review_rejects", 0) >= 1:
                return {"review_rejects": 1, "review_status": "rejected"}
            msg(state["session_id"], "assistant",
                "[review·重写] 首次 reject：以新快照受控重写报告一次（硬上限 1，复审后必收敛）")
            res_m2 = registry.invoke(tid, skills["collect_metrics"], {})
            if res_m2.status == "ok" and "no_report" not in (res_m2.output or "{}"):
                metrics2 = json.loads(res_m2.output)
                registry.invoke(tid, skills["gen_report"], {"metrics": metrics2, "rtm": rtm})
                res_v2 = registry.invoke(tid, skills["review_report"],
                                         {"metrics": metrics2, "verdict": verdict})
                rev2 = json.loads(res_v2.output) if res_v2.status == "ok" else {"status": "reject"}
                msg(state["session_id"], "assistant",
                    f"[review·复审] {rev2['status']}（受控重写后终审，无论结果收敛）")
                status = "pass" if rev2["status"] == "pass" else "rejected"
                return {"review_rejects": 1, "review_status": status}
            return {"review_rejects": 1, "review_status": "rejected"}
        return {"review_status": "pass"}

    return {"clone": clone, "analyze": analyze, "plan": plan, "provision": provision,
            "generate": generate, "run": run, "evaluate": evaluate, "report": report,
            "review": review, "static_draft": static_draft}
