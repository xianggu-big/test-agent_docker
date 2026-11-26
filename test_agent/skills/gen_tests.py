"""gen_tests —— 按五种方法知识库生成用例：写入工作区 → 回灌容器 → 落库用例库。

流程：
1. 收集被测源码（排除测试文件与噪声目录，限总量防 token 爆炸）
2. 组装"五种方法骨架"提示词，调 DeepSeek 生成 JSON 用例清单
3. 解析 + 清洗（只保留 def test_ 开头的完整函数），写入 test_gen_r<N>.py
4. 重新快照进容器（下一轮 run_tests 直接可跑）
5. test_scripts + test_cases 落库（source='ai'，method=方法标签）—— 论文用例库数据源
"""
import ast
import hashlib
import json
import logging
import re
from pathlib import Path

from ..skills.llm_client import make_chat

from .. import config
from ..graph.repair import CASE_OWN_DEFECT_ERRORS
from ..store.db import get_conn
from .llm_usage import log_llm_call
from .prompt_library import (CROSS_REVIEW_SYSTEM, MAX_SOURCE_CHARS, SYSTEM_PROMPT,
                            system_prompt_with_isolation,
                             build_cross_review_prompt, build_regen_prompt,
                             build_user_prompt)
from .test_templates import expand_template_cases

log = logging.getLogger(__name__)


def _sanitize_name(name: str, idx: int, used: set[str]) -> str:
    """把 LLM 自报的用例名规整成合法且唯一的函数名。

    A5 纪律：写文件前统一用这个名字重写 def，保证 test_cases.name 与
    pytest nodeid 一一对应——归因/定向修复（②）按 nodeid 查用例的前提。
    """
    base = re.sub(r"\W+", "_", (name or "").strip()).strip("_") or f"test_case_{idx}"
    if not base.startswith("test_"):
        base = "test_" + base
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


SOURCE_NOISE_FILES = {"setup.py", "setup.cfg", "conftest.py", "tests.py", "test.py"}
WHOLE_FILE_LIMIT = 12000  # 不超过该大小的文件整文件收录（上下文最完整；模块级词表/正则不丢）

# P1-4a（2026-10-31 体检）：生成物 import 白名单——LLM 输出的 import 原样进容器执行，
# 被测仓库提示注入可诱导 import os/subprocess/socket 等触达容器外。允许集=测试框架/
# 标准库无害子集/被测模块本身；白名单外一律剔除并留理由（确定性、零成本）。
IMPORT_WHITELIST = {
    # 测试与常用无害三方
    "pytest", "unittest", "mock", "jsonschema", "hypothesis", "freezegun",
    # 标准库无害子集（数据/文本/时间/数学——不含 os/sys/subprocess/socket/pathlib/shutil 等）
    "math", "random", "string", "textwrap", "datetime", "time", "calendar",
    "collections", "itertools", "functools", "re", "json", "decimal", "fractions",
    "statistics", "enum", "abc", "copy", "heapq", "bisect", "uuid", "base64",
    "hashlib", "typing", "dataclasses", "contextlib", "warnings", "types",
}


def _is_noise_source(p: Path) -> bool:
    """打包/配置/测试运行器文件不是被测业务代码。

    D5/D1②（2026-10-30 H1 实锤）：setup.py 曾按路径序挤占预算成为 LLM 唯一见到的"源码"，
    tests.py 曾被当被测源码收录。"""
    if p.name.lower() in SOURCE_NOISE_FILES:
        return True
    if p.name.startswith("TestGen") and p.name.endswith(".php"):
        return True   # 白包阶段4：php 生成物不进被测源码
    return p.name.startswith("test_") or p.name.endswith("_test.py") \
        or p.name.endswith("_test.go")   # 白包阶段3：go 生成物/自带测试不进被测源码


JS_SOURCE_EXTS = (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
JS_NOISE_SUFFIXES = (".test.js", ".spec.js", ".test.ts", ".spec.ts", ".d.ts", ".min.js",
                     ".config.js", ".config.ts")
# jest 用例声明：test("test_xxx" / it('test_xxx'（提示词强制名字以 test_ 开头无空格）
JS_CASE_RE = re.compile(r'\b(?:test|it)\s*\(\s*[\'"]test_\w+')

# task_366 实锤：12 条用例全部写 path.join(__dirname, '..')（jest 惯例假设用例在 test/
# 子目录），但平台把用例文件直接写在仓库根 → 容器内 ROOT='/' → 全部被测脚本
# "Cannot find module" → exit 1 + stdout 空 → 12 条被误归因"疑似产品缺陷"。
# 平台布局下该形态 100% 指向仓库外，等价修正=去掉 '..' 段（LLM 本意=仓库根）；
# 只改路径脚手架，不碰断言与用例逻辑。上跳两级等更深的形态无歧义修正语义，不在此
# 处理（提示词布局纪律防其出现；真出现时 stderr 诊断段可见，见 js_adapter）。
_JS_ROOT_SELF = (
    r"path\.(?:join|resolve)\(\s*__dirname\s*,\s*(['\"])\.\.\1\s*\)")
# 后续段不含 '..'（tempered：上跳两级等更深形态语义不明，不动）且不含括号（排除嵌套调用）
_JS_ROOT_DOTDOT = (
    r"path\.(?:join|resolve)\(\s*__dirname\s*,\s*(['\"])\.\.\1\s*,\s*"
    r"((?:(?!\.\.)[^()])*?)\)")


def _fix_js_scaffold_paths(code: str) -> tuple[str, int]:
    """JS 用例路径脚手架确定性修正：path.join(__dirname, '..') → __dirname、
    path.join(__dirname, '..', x) → path.join(__dirname, x)。
    返回 (修正后代码, 修正处数)。零成本纯字符串变换，修正处由返回 JSON root_fixed 留痕。"""
    n = len(re.findall(_JS_ROOT_SELF, code))
    code = re.sub(_JS_ROOT_SELF, "__dirname", code)
    code, more = re.subn(_JS_ROOT_DOTDOT, r"path.join(__dirname, \2)", code)
    return code, n + more


def _is_js_noise(p: Path) -> bool:
    """JS 项目的非被测文件：测试/声明/压缩/配置/生成物。"""
    name = p.name.lower()
    if name.endswith(JS_NOISE_SUFFIXES) or name.startswith("test_gen_"):
        return True
    return name in ("jest.config.js", "babel.config.js", "webpack.config.js", "vite.config.js",
                    "jest.config.ts")


def gen_file_naming(lang: str, round_no: int, feedback: bool = False, fb_no: int = 0) -> tuple[str, str]:
    """生成物命名分流（渲染层纯函数，白包阶段1抽出）：py→test_gen_r{N}.py；
    js→.test.js；ts→.test.ts（jest testMatch 同默认口径）。返回 (文件名, framework)。"""
    if lang in ("javascript", "typescript"):
        ext = ".test.ts" if lang == "typescript" else ".test.js"
        framework = "jest"
    elif lang == "java":
        # maven surefire 只扫 src/test/java 下 *Test 类；相对路径当 fname 用（写 ws/fname）
        base = f"src/test/java/TestGenR{round_no}.java"
        if feedback and fb_no:
            base = f"src/test/java/TestGenFb{fb_no}.java"
        return base, "junit5"
    elif lang == "php":
        # phpunit 显式文件运行（TestGen*.php 不匹配默认 Test.php 发现后缀，必须传文件）；
        # 类名与文件名一致（渲染壳保证）——白包阶段4
        base = f"TestGenR{round_no}.php"
        if feedback and fb_no:
            base = f"TestGenFb{fb_no}.php"
        return base, "phpunit"
    elif lang == "go":
        # go test 只认 *_test.go（同包根目录）；白包阶段3。go test 无文件级隔离参数
        # （R-Loop 轮全包重跑，与 java 同语义边界，诚实入档）
        base = f"TestGenR{round_no}_test.go"
        if feedback and fb_no:
            base = f"TestGenFb{fb_no}_test.go"
        return base, "go_test"
    else:
        ext, framework = ".py", "pytest"
    if feedback and fb_no:
        return f"test_gen_fb{fb_no}{ext}", framework
    return f"test_gen_r{round_no}{ext}", framework


def _repo_probe_payload(ws):
    """方案 D+1（2026-11-23 真仓库破局）：旗标开时从被测工作区抽线束卡+盲意图骨架
    （+调用点），关={}（build_user_prompt 默认参数=提示词逐字节不变）。
    抽取只读 workspace（buggy 快照副本）——三道泄漏门禁在 tests/test_no_leakage.py。
    Python-only（AST 抽壳器 py 实现）；fail-open：抽取失败不阻断生成主链路。"""
    if not config.REPO_PROBE_ENABLED or ws is None:
        return {}
    try:
        import importlib.util as _ilu
        root = Path(config.ROOT)

        def _load(name, rel):
            spec = _ilu.spec_from_file_location(name, root / rel)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        hc_mod = _load("harness_card", "scripts/harness_card.py")
        out = hc_mod.extract_card(Path(ws))
        card = out.get("card") if out.get("status") == "ok" else None
        skel = cs = None
        if config.REPO_PROBE_INTENT:
            is_mod = _load("intent_skeleton", "scripts/intent_skeleton.py")
            iso = is_mod.extract(Path(ws), None)
            if iso.get("status") == "ok":
                skel, cs = iso["skeletons"], iso["call_sites"]
        log.info("gen_tests repo_probe 注入：card=%s skel=%s call_sites=%s",
                 bool(card), skel and len(skel), cs and len(cs))
        return {"harness_card": card, "intent_skeletons": skel, "call_sites": cs}
    except Exception as e:      # noqa: BLE001 —— fail-open
        log.warning("repo_probe 抽取失败（fail-open）：%s", e)
        return {}


def _load_trapkb_v2() -> str:
    """动态陷阱库 v2 注入段加载（E10 Arm 1，2026-11-16；默认关=空串提示词逐字节不变）。
    产物=build_trapkb_v2.py 的历史失败聚类（signature/size/top_messages）——渲染 top
    簇失败消息作为「历史高频失败模式」警示段。旗标关/文件缺/解析坏 → 空串（fail-open）。"""
    if not config.TRAPKB_V2_ENABLED:
        return ""
    try:
        data = json.loads(Path(config.TRAPKB_V2_PATH).read_text(encoding="utf-8"))
        traps = (data.get("traps") or [])[:6]
        if not traps:
            return ""
        lines = ["\n## 历史高频失败模式（动态陷阱库 v2：本项目历史批量运行的真实失败聚类）",
                 "以下失败模式在历史运行中高频出现，生成用例时优先覆盖这些边界与调用形态："]
        for t in traps:
            msg = (t.get("top_messages") or ["（无样本消息）"])[0]
            lines.append(f"- 【历史 {t['size']} 例】{msg}")
        return "\n".join(lines) + "\n"
    except Exception as e:      # noqa: BLE001 —— 陷阱库失败不炸生成主链路
        log.warning("trapkb_v2 加载失败（%s: %s）——空串降级", type(e).__name__, e)
        return ""


def collect_gate_cmd(lang: str, fname: str) -> list[str]:
    """收集闸命令分流（纯函数，白包阶段1）：py=pytest --collect-only；js=node --check；
    ts=tsc --noEmit 类型闸——node --check 解析不了 TS 类型标注（任务卡 S4 差异点）。
    CLI 文件模式不带 --strict（宽容口径）；--types jest + 全局 typeRoots（容器预检实锤：
    文件模式不会自动加载 /usr/local/lib/node_modules 下的 @types/jest，缺它 test/expect
    全部 TS2304 未定义）。被 SUT 自身类型错误连坐属已知边界，如实废案不误报成功。
    2026-11-17 multi60 卡 2（1295 ts-ky 实锤）：文件模式忽略项目 tsconfig（TS 语义）
    → 旧 --target es2020 默认 lib 对现代库源码（Object.hasOwn=ES2022）恒 TS2550 →
    生成物被 SUT 源码连坐三轮废案熔断。升 target es2022 + 显式 lib es2022,dom——
    类型闸拦截力不变（生成代码类型错仍全抓），只是不再用落后于时代的标准库冤枉健康项目。"""
    if lang == "typescript":
        # 2026-11-17 multi60 实锤（1352 nestjs）：被测项目源码用装饰器（@Injectable）
        # 而闸命令未开 experimentalDecorators → TS1206 被测源码连坐整案废案。
        # 补装饰器两选项（nestjs/类框架项目的编译前提）。
        return ["tsc", "--noEmit", "--skipLibCheck", "--esModuleInterop", "--module",
                "commonjs", "--target", "es2022", "--lib", "es2022,dom",
                "--experimentalDecorators", "--emitDecoratorMetadata",
                "--moduleResolution", "node",
                "--types", "jest", "--typeRoots", "/usr/local/lib/node_modules/@types",
                fname]
    if lang == "javascript":
        return ["node", "--check", fname]
    if lang == "java":
        # 白包阶段2：编译闸 mvn test-compile（测试代码与主代码一起编译；闸不过=生成缺陷整案废案）。
        # fname 不进命令——maven 编译全项目；任务卡写 `mvn -q compile`，测试代码需 test-compile（偏差入档）。
        return ["mvn", "-q", "test-compile"]
    if lang == "go":
        # 白包阶段3：编译闸 go test -c（编译被测主包+测试文件，不执行）。
        # 2026-11-16 全语言实锤（1207 go-ver-gate）：原命令带 go build ./...——对
        # "package main 无 main 函数"的纯函数库 demo 恒失败 → gen 闸每轮废案 →
        # 0/0"通过但覆盖率 0%"死循环熔断。test -c 本身编译主包（主代码编译错
        # 一样炸=等效拦截），去掉 build ./... 不降低拦截力且不误杀库形态。
        return ["sh", "-c", "go test -c -o /tmp/mta_gate.bin ."]
    if lang == "php":
        # 白包阶段4：语法闸 php -l（闸不过=生成缺陷整案废案）；编译/运行闸=phpunit 本身
        return ["php", "-l", fname]
    return ["pytest", "--collect-only", "-q", fname]


def _go_package_of(ws: Path) -> str:
    """ws 根首个非测试 .go 文件的 package 子句（go test 同包前提；找不到回退 main）。"""
    for p in sorted(ws.glob("*.go")):
        if p.name.endswith("_test.go"):
            continue
        try:
            m = re.search(r"^package\s+(\w+)", p.read_text(encoding="utf-8",
                                                            errors="ignore"), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return "main"


def _normalize_go_imports(imports_text: str) -> list[str]:
    """LLM imports 字段 → import() 块行清单（每行一条 import 语句剥前缀补引号）；
    空回退最小 "testing"（*testing.T 签名必需）。"""
    out: list[str] = []
    for ln in (imports_text or "").splitlines():
        ln = ln.strip()
        if not ln or ln in ("import", "import (", ")", "(", ")"):
            continue
        ln = re.sub(r"^import\s+", "", ln).strip()
        if ln in ("(", ")"):
            continue
        if ln and not ln.startswith('"'):
            ln = f'"{ln}"'      # 裸路径补引号（LLM 常写 import testing）
        out.append(ln)
    return out or ['"testing"']


# —— Go 漏 import 自检自补（2026-11-08 658 实锤：LAYER_AWARE 格 AI 用例写出
# base64.RawURLEncoding 却漏 import encoding/base64 → 整包 build failed → 8 条用例
# 0 条执行 → 检出率假 0（用例断言已覆盖全部四条检出方向却没跑起来）。
# 落库前确定性补齐，零 LLM 零容器；标识符点调用文本命中才补=补了必然被用，
# 不会引入 "imported and not used"；项目内包/第三方不在映射表=不补。）
_GO_STD_IMPORT_HINTS = {
    "base64.": '"encoding/base64"',
    "strings.": '"strings"',
    "bytes.": '"bytes"',
    "json.": '"encoding/json"',
    "http.": '"net/http"',
    "httptest.": '"net/http/httptest"',
    "time.": '"time"',
    "fmt.": '"fmt"',
    "os.": '"os"',
    "errors.": '"errors"',
    "strconv.": '"strconv"',
    "sort.": '"sort"',
    "io.": '"io"',
    "url.": '"net/url"',
    "sql.": '"database/sql"',
    "context.": '"context"',
    "rand.": '"math/rand"',
}


def _go_import_pkg_name(line: str) -> str:
    """import 行的包名（别名优先，裸路径取末段）：`alias "path"`→alias；`"a/b/c"`→c。"""
    m = re.match(r'(?:([\w]+)\s+)?"([^"]+)"', line.strip())
    if not m:
        return ""
    if m.group(1):
        return m.group(1)
    return m.group(2).rstrip("/").rsplit("/", 1)[-1]


def _strip_go_undefined_calls(content: str, sources_text: str = "") -> tuple[str, list[str]]:
    """Go 用例幻觉辅助函数手术（2026-11-08 668 实锤：用例调用 newFakeRepo() 却没
    定义它 → undefined 整包 build failed → R-Loop 3 轮同指纹熔断）。
    裸调用（非 pkg.Func 形式）不在「本文件定义 ∪ 被测源码定义」→删除包含它的测试
    函数（删 1 保 N：一个幻觉函数不再一刀切死整包）。sources_text=被测源码全文
    （项目内函数是合法调用目标——668 复盘发现 NewService 这类项目函数必须白名单，
    否则手术误杀真实调用）。testing 方法（t.Errorf 等）带接收者=非裸调用天然排除。"""
    m = re.search(r"import \(\n(.*?)\n\)", content)
    if not m:
        return content, []
    body_start = m.end()
    body = content[body_start:]
    defined = set(re.findall(r"func\s+(?:\([\w ]+\*?[\w ]+\)\s+)?(\w+)\s*\(", body))
    defined |= set(re.findall(r"func\s+(?:\([\w ]+\*?[\w ]+\)\s+)?(\w+)\s*\(",
                              sources_text or ""))
    # 裸调用：前面不是点/字母/下划线/引号（排除 pkg.Func、receiver.Method、字符串）
    calls = set(re.findall(r"(?<![.\w\"/])([a-zA-Z_]\w*)\s*\(", body))
    builtin = {"len", "cap", "make", "new", "append", "copy", "delete", "panic"}
    undefined = {c for c in calls
                 if c not in defined and c not in builtin and not c.startswith("Test")}
    if not undefined:
        return content, []
    # 删除包含未定义调用的 func TestXxx 块（到下一个顶层 func 边界）
    removed, out = [], []
    blocks = re.split(r"(?=^func\s)", body, flags=re.M)
    header = content[:body_start]
    for blk in blocks:
        mm = re.match(r"func\s+Test", blk)
        if mm and any(f"{u}(" in blk for u in undefined):
            named = re.match(r"func\s+(Test\w*)", blk)
            removed.append(named.group(1) if named else "?")
        else:
            out.append(blk)
    if not removed:
        return content, []
    return header + "".join(out), removed


def _fix_go_missing_imports(content: str) -> tuple[str, list[str]]:
    """Go 用例 import 双向修复（落库前确定性，零 LLM 零容器）：
    ①补缺（658 实锤）：代码区「标识符+点」命中而 import 块缺对应标准库包→补块尾；
    ②删多余（660 实锤）：import 块里整段代码零使用的包→删行——Go 的 unused import
    是编译错误（Python/JS 只是警告），LLM 从其他语言习惯带多余 import=整包 build
    failed 一刀切死全部用例。
    占用检查（659 实锤）：项目内包占用同包名（internal/errors vs 标准库 errors）时禁补。
    `_ "x"` / `. "x"` 侧效应导入不参与检查。返回 (新 content, 补了哪些包)。"""
    m = re.search(r"import \(\n(.*?)\n\)", content, re.S)
    if not m:
        return content, []
    code_part = content[m.end():]

    # ① 删多余：整段代码（含注释/字符串——保守方向宁误留不误删）零使用的包
    kept, removed = [], []
    for ln in m.group(1).splitlines():
        s = ln.strip()
        name = _go_import_pkg_name(s)
        if (not name or s.startswith(("_ ", ". "))
                or re.search(rf"\b{re.escape(name)}\s*[.(]", code_part)):
            kept.append(ln)
        else:
            removed.append(s)
    have = {f'"{x}"' for x in re.findall(r'"([^"]+)"', "\n".join(kept))}
    taken_names = {_go_import_pkg_name(ln) for ln in kept} - {""}

    # ② 补缺：标识符点调用命中且未 import 且包名未被占用
    added = []
    for ident, pkg in _GO_STD_IMPORT_HINTS.items():
        if pkg in have:
            continue
        if _go_import_pkg_name(pkg) in taken_names:
            continue
        if re.search(rf"(?<![\"\w./]){re.escape(ident)}", code_part):
            added.append(pkg)

    if not added and not removed:
        return content, []
    new_block = "\n".join(kept) + "".join(f"\n\t{ln}" for ln in sorted(added))
    return content[:m.start(1)] + new_block + content[m.end(1):], added


def _iter_source_files(ws: Path, lang: str = "python"):
    """按目标语言枚举被测源码：python=*.py；javascript/typescript=JS_SOURCE_EXTS；
    java=*.java（白包阶段2）；go=*.go（白包阶段3，排除 _test.go 生成物）；
    php=*.php（白包阶段4，排除 TestGen* 生成物与 vendor/）。"""
    exts = (".java",) if lang == "java" else (
        ".go",) if lang == "go" else (
        ".php",) if lang == "php" else (
        JS_SOURCE_EXTS if lang in ("javascript", "typescript") else (".py",))
    if lang == "go":
        pass  # _test.go 过滤在循环内（fname 形态判断）
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if lang in ("javascript", "typescript"):
            if _is_js_noise(p):
                continue
        elif _is_noise_source(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.strip():
            yield p, text


def _split_top_level(text: str) -> list[tuple[str, str]]:
    """大文件按顶层 def/class 切片，模块级赋值（词表/正则等常量）并入 "(module-level)" 段。

    D1①：more.py 176KB 这类文件整收必超预算，按函数切片才能进 LLM 视野；
    常量段单独保留——titlecase 的 SMALL_WORDS 词表决定行为，丢了等于继续盲写。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines(keepends=True)

    def seg(node) -> str:
        end = getattr(node, "end_lineno", None) or node.lineno
        return "".join(lines[node.lineno - 1:end]).strip()

    chunks: list[tuple[str, str]] = []
    globals_seg: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s = seg(node)
            if s:
                chunks.append((node.name, s))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            s = seg(node)
            if s:
                globals_seg.append(s)
    if globals_seg:
        chunks.insert(0, ("(module-level)", "\n\n".join(globals_seg)))
    return chunks


_ROOT_MANIFESTS = ("package.json", "go.mod", "requirements.txt",
                   "pom.xml", "composer.lock", "composer.json")


def _count_product_sources(sources: list[tuple[str, str]]) -> int:
    """非根级清单的收录数（产品源码）——A3 零收录回退的判定依据：只剩清单文件
    =锁定范围内产品源码 0 收录（scope 前缀与真实路径失配）。"""
    return sum(1 for rel, _ in sources if rel not in _ROOT_MANIFESTS)


def _collect_sources_detailed(ws: Path, lang: str = "python",
                              scope_paths: list[str] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """被测源码收集（带跳过台账）：小文件整收、大文件按顶层符号切片、预算截断如实记录。

    JS/TS 无 Python AST：不切片、按预算顺序整收，超出的文件截断尾部并留台账（诚实呈现）。
    scope_paths（2026-11-18 索引路由）：非空时只收命中模块的文件（根级清单文件照留）——
    索引路由锁定的源码范围（设计方案-项目知识索引-20261117）。"""
    sources: list[tuple[str, str]] = []
    skipped: list[str] = []
    used = 0
    is_js = lang in ("javascript", "typescript")
    is_java = lang == "java"
    is_go = lang == "go"    # 白包阶段3：go 无 Python AST，与 JS/java 同走整收分支
    is_php = lang == "php"   # 白包阶段4：php 无 Python AST，与 JS/java 同走整收分支
    for p, text in _iter_source_files(ws, lang):
        rel = p.relative_to(ws).as_posix()
        if scope_paths:
            # 索引路由锁定范围（2026-11-18）：rel 命中任一 scope 模块目录才收录
            # （按目录段匹配——rstrip("/") 后要求 "/ 边界"，防 "user" 误吞 "users"）；
            # 根级清单文件（package.json/go.mod/…）始终保留
            keep_root = rel in _ROOT_MANIFESTS
            sp_dirs = [sp.replace("\\", "/").rstrip("/") for sp in scope_paths]
            if not keep_root and not any(
                    rel == sp or rel.startswith(sp + "/") for sp in sp_dirs):
                skipped.append(f"{rel}（索引路由范围外）")
                continue
        if is_java or is_js or is_go or is_php:
            # JS：无 AST 切片。语法可粗检（花括号平衡）不拦——语法闸在容器内 node --check。
            budget_left = MAX_SOURCE_CHARS - used
            if len(text) > budget_left:
                kept = text[:budget_left]
                sources.append((rel + "（预算截断）", kept + "\n/* …（超出预算截断，完整文件见仓库）*/"))
                skipped.append(f"{rel}（{len(text)} 字符超预算，截断收录 {len(kept)}）")
                used += budget_left
            else:
                sources.append((rel, text))
                used += len(text)
            continue
        if len(text) > WHOLE_FILE_LIMIT or used + len(text) > MAX_SOURCE_CHARS:
            # 大文件或预算吃紧：按顶层 def/class 切片逐段收录（放得下多少收多少）
            placed = False
            for name, chunk in _split_top_level(text):
                key = f"{rel}::{name}"
                if used + len(chunk) > MAX_SOURCE_CHARS:
                    skipped.append(f"{key}（预算截断）")
                    continue
                sources.append((key, chunk))
                used += len(chunk)
                placed = True
            if not placed:
                skipped.append(f"{rel}（整文件 {len(text)} 字符超预算且无可容纳切片）")
            continue
        try:
            ast.parse(text)
        except SyntaxError:
            # 语法坏文件喂给 LLM 是浪费预算且必然产出废用例——如实记台账
            skipped.append(f"{rel}（语法不可解析，未收录）")
            continue
        sources.append((rel, text))
        used += len(text)
    # A3（2026-11-20）：scope 锁定后产品源码零收录（只剩根级清单）= scope 前缀与
    # 真实路径失配——显式回退全库重收（不静默空转；Qoder 体检论断 3 的复现形态：
    # src/user 布局锁定 user/ → 0 收录 → "未发现可测源码"）。台账注记供
    # generate_tests 检测打 scope_fallback 标（nodes.generate 清空路由锁定）。
    if scope_paths and _count_product_sources(sources) == 0:
        note = ("[index-scope] 锁定范围产品源码 0 收录——已回退全库重收"
                f"（scope={list(scope_paths)}，前缀失配，A3 留痕）")
        sources, skipped = _collect_sources_detailed(ws, lang, scope_paths=None)
        return sources, [note] + skipped
    return sources, skipped


def _collect_sources(ws: Path, lang: str = "python") -> list[tuple[str, str]]:
    """被测源码（兼容壳，repair_strategy 复写路径共用）；需要跳过台账用 _collect_sources_detailed。"""
    return _collect_sources_detailed(ws, lang)[0]


def source_outline(ws: Path, lang: str = "python") -> dict | None:
    """被测源码结构清单（BREADTH 广度采样用，config.BREADTH_SAMPLING_ENABLED 时才调用）。

    返回 {"files": ["rel::fn1,fn2", ...], "functions": {函数名集}}；解析失败 fail-open 返回 None
    （清单缺失不拦主链路——信息提供类手段，失败退化为无清单的旧行为）。"""
    try:
        files: list[str] = []
        functions: set[str] = set()
        is_js = lang in ("javascript", "typescript")
        for p, text in _iter_source_files(ws, lang):
            rel = p.relative_to(ws).as_posix()
            if lang == "java":
                # 白包阶段2：java 方法签名正则（public/private static 等修饰符宽容）
                names = re.findall(r"(?:public|private|protected)?\s*(?:static\s+)?"
                                   r"[\w<>\[\]]+\s+([A-Za-z_][\w$]*)\s*\(", text)
                functions.update(names)
                files.append(f"{rel}::{('、'.join(dict.fromkeys(names))[:12]) or rel}")
                continue
            if lang == "go":
                # 白包阶段3：go 函数签名正则（func Name(...)/func (r *T) Name(...)，剥 Test 前缀生成物）
                names = [n for n in re.findall(
                    r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\(", text)
                    if not n.startswith("Test")]
                functions.update(names)
                files.append(f"{rel}::{('、'.join(dict.fromkeys(names))[:12]) or rel}")
                continue
            if lang == "php":
                # 白包阶段4：php 函数签名正则（function name(...)，剥 test 前缀）
                names = [n for n in re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", text)
                         if not n.lower().startswith("test")]
                functions.update(names)
                files.append(f"{rel}::{('、'.join(dict.fromkeys(names))[:12]) or rel}")
                continue
            if is_js:
                names = re.findall(r"(?:function\s+|const\s+|let\s+|var\s+)"
                                   r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\()",
                                   text) or re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", text)
            else:
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                names = [n.name for n in tree.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            names = list(dict.fromkeys(names))[:12]
            if names:
                functions.update(names)
                files.append(f"{rel}::{'、'.join(names)}")
            else:
                files.append(rel)
        if not files:
            return None
        return {"files": files, "functions": functions}
    except Exception:  # noqa: BLE001 —— fail-open
        return None


def _condition_function_domains(conds: list[dict], functions: set[str]) -> set[str]:
    """条件文本命中的源码函数名集合（函数域归属判定；广度补充闸用）。"""
    text = " ".join(f"{c.get('condition') or ''} {c.get('method_hint') or ''}" for c in conds)
    return {fn for fn in functions if fn and fn in text}


def _downsample_conditions(conditions: list[dict], ws: Path, lang: str,
                           task_id: int) -> tuple[list[dict], int]:
    """条件超限降采样（generate_tests 主链路与对话增补共用，可单测纯函数）。

    关（默认）：按优先级保 GEN_MAX_CASES 条（2026-11-08 用户指令默认 10——小步验证
    跑通整线勿铺量；历史口径 15/20 可经 MTA_GEN_MAX_CASES 上调回）；开
    （BREADTH_SAMPLING_ENABLED）：优先级保底后「未覆盖函数域分散补充」各 1 条，
    总上限仍 GEN_MAX_CASES——治同函数域挤占名额。"""
    cap = max(1, int(config.GEN_MAX_CASES))
    conditions_truncated = 0
    if conditions and len(conditions) > cap:
        prio = {"高": 0, "中": 1, "低": 2}
        conditions.sort(key=lambda c: prio.get((c.get("priority") or "中"), 1))
        conditions_truncated = len(conditions) - cap
        rest = conditions[cap:]
        conditions = conditions[:cap]
        if config.BREADTH_SAMPLING_ENABLED and rest:
            outline = source_outline(ws, lang) or {}
            functions = outline.get("functions") or set()
            if functions:
                covered = _condition_function_domains(conditions, functions)
                extra = []
                for c in rest:
                    if len(extra) >= 5:
                        break
                    doms = _condition_function_domains([c], functions)
                    if doms and doms.isdisjoint(covered):
                        extra.append(c)
                        covered |= doms
                if extra:
                    conditions.extend(extra)
                    conditions_truncated -= len(extra)
                    log.info("gen_tests task=%s 广度补充 %d 条（函数域分散，总 %d 条）",
                             task_id, len(extra), len(conditions))
        log.warning("gen_tests task=%s 条件 %d 条超限，按优先级保 %d（截 %d）",
                    task_id, conditions_truncated + len(conditions), cap,
                    conditions_truncated)
    return conditions, conditions_truncated


# 模型漂移第 3/4 形（2026-11-05 批4 12/12 降级实锤）：JSON 字符串值位置写 Python
# 表达式——重复 "A" * 65、拼接 "Ab1!" + "aaa"。合法 JSON 中字符串结束引号后只可能
# 是 , } ] :，出现 + / * 必然非法，因此片段级求值替换不会误伤 code 字段内嵌的
# '-' * 20（其宿主字符串的结束引号后跟的是 , 或 }，模式匹配不到它）。
_PY_STR_EXPR_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"(?:\s*[+*]\s*(?:"(?:[^"\\]|\\.)*"|\d+))*')


def _eval_str_expr(m: "re.Match") -> str:
    """字符串表达式片段求值：片段经正则约束只含字符串/数字字面量与 + *（无名称、
    无调用、无下标），eval 无注入面；求值失败保持原样交外层容错循环。"""
    try:
        val = eval(m.group(0), {"__builtins__": {}}, {})   # noqa: S307 —— 见上，无注入面
        return json.dumps(val if isinstance(val, str) else str(val))
    except Exception:                                      # noqa: BLE001
        return m.group(0)


def _salvage_json_output(text: str) -> dict | None:
    """两级降级提取（2026-11-17 multi60 卡 2，task1278 三份留档实证，仅标准路径全败后调用）。

    形态①（留档 005545/005738）：JSON 本体完整但前后带 markdown 附录——辅助类 ```java
    块或二次 ```json 块。主路径贪婪 \\{.*\\} 抓到附录里的 `}` → Extra data；
    解法=从首个 `{` 起用 raw_decode 解首个完整对象（解码器自带括号/字符串配平，
    对象结束即停，附录天然排除）。
    形态②（留档 005906）：输出在 cases 数组中部截断（DeepSeek 默认 max_tokens≈4K，
    25-30KB 处 Expecting ',' delimiter）——解法=从尾部向前找对象边界 `}` 截尾 + 补
    "]}" 闭合，以"能否解析"做安全验证（切在字符串字面量内部必解析失败，天然防误切）。
    诚实纪律：只提取模型真实输出的完整对象/完整条目，绝不补造缺失字段。"""
    text = text or ""
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]
    # 形态①：首个完整对象
    for decoder in (json.JSONDecoder(), json.JSONDecoder(strict=False)):
        try:
            obj, _end = decoder.raw_decode(s)
            if isinstance(obj, dict) and isinstance(obj.get("cases"), list):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    # 形态②：截断截尾闭合——`}` 位置从尾向头逐个试（上限 300 防病态输入），
    # 候选只补缺失的数组/对象闭合，不追加任何内容
    positions = [i for i, ch in enumerate(s) if ch == "}"]
    for i in reversed(positions[-300:]):
        cand = s[:i + 1] + "]}"
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            try:
                data = _loads_tolerant(cand)
            except (json.JSONDecodeError, ValueError):
                continue
        if isinstance(data, dict) and isinstance(data.get("cases"), list) and data["cases"]:
            return data
    return None


def _loads_tolerant(s: str):
    """JSON 宽容解析：DeepSeek 模型更新后偶发在 JSON 里输出 Python 形态（2026-10-30
    D8 实锤 True/False/None 字面量；2026-11-05 批4 实锤字符串重复/拼接表达式）。
    【A0-3 修订 2026-11-27】先标准 json.loads——合法输入零变形直返（旧版先做表达式
    替换，把合法 JSON 的中文字符串转义撑大，2680 实测 135→250 字符）；失败才进修复链。
    Extra data（对象后拖尾冗余——task 2680/2688 原文实锤句尾杂散 `}`，两次不可解析
    →hard_error，S-K1=44/45 的唯一败因）用 raw_decode 取首个完整对象救回，与
    _salvage_json_output 形态①同哲学。字面量替换只在解析器报错位置做——报错点必然
    在字符串外，因此 code 字符串里的 "assert x is True" 不会被误伤；至多 50 次防病态输入。"""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    s = _PY_STR_EXPR_RE.sub(_eval_str_expr, s)
    for _ in range(50):
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            if e.msg == "Extra data":
                try:
                    obj, _end = json.JSONDecoder().raw_decode(s)
                    return obj
                except json.JSONDecodeError:
                    pass
            try:
                # 本地小模型常见形态（qwen3:4b-instruct 实测）：字符串值内裸换行等
                # 未转义控制字符——strict=False 只放开这一点，安全面不变；
                # 若同时存在 Python 字面量问题，strict=False 仍失败，落入下方字面量替换
                return json.loads(s, strict=False)
            except json.JSONDecodeError:
                pass
            seg = s[e.pos:e.pos + 5]
            for lit, js in (("True", "true"), ("False", "false"), ("None", "null")):
                if seg.startswith(lit):
                    s = s[:e.pos] + js + s[e.pos + len(lit):]
                    break
            else:
                raise
    raise ValueError("tolerant parse loop limit")


def _parse_cases(text: str, lang: str | None = None) -> dict | None:
    """从 LLM 回复中鲁棒提取 JSON；解析失败返回 None（诚实降级，不硬造用例）。

    用例两种合法形态（双通道）：①自由代码（code 含 def test_）②槽位模板
    （template+slots，由 test_templates 展开）——解析层两种都收，展开在后续步骤。
    lang 为 JS/TS 时（白包阶段1 收紧）：只认 jest 形态——LLM 会把五方法知识库的
    Python 示例照抄进 code（ts-calc 真机三轮废案实锤），def test_ 命中旧过滤放行
    混写、直到文件级 tsc 闸整案废案；解析层剔除让干净用例存活，pytest 槽位模板
    （渲染 def test_）对 JS/TS 同样不适用。
    """
    m = re.search(r"\{.*\}", text or "", re.S)   # None 容错：content=None 诚实降级不 TypeError
    if not m:
        return None
    try:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = _loads_tolerant(m.group(0))   # Python 字面量宽容重试（模型 JSON 合规性漂移）
    except (json.JSONDecodeError, ValueError):
        # 标准路径全败的降级提取（2026-11-17 卡 2，1278 三份留档实证）：①JSON 完整但
        # 带附录（贪婪匹配误伤→raw_decode 首对象）②cases 中部截断（截尾闭合救回完整
        # 条目）。救不回仍 None（诚实降级，不硬造用例）。
        data = _salvage_json_output(text)
        if data is None:
            return None
    cases = data.get("cases")
    if not isinstance(cases, list):
        return None
    if lang in ("javascript", "typescript"):
        cleaned = [c for c in cases if isinstance(c, dict)
                   and isinstance(c.get("code"), str) and JS_CASE_RE.search(c["code"])]
    elif lang == "java":
        # 白包阶段2：JUnit5 形态（@Test 注解或 void test_ 方法）——混写 Python/jest 在解析层剔除
        cleaned = [c for c in cases if isinstance(c, dict)
                   and isinstance(c.get("code"), str) and JAVA_CASE_RE.search(c["code"])]
    elif lang == "go":
        # 白包阶段3：go test 形态（func TestXxx(t *testing.T)）——混写 Python/jest/JUnit 在解析层剔除
        cleaned = [c for c in cases if isinstance(c, dict)
                   and isinstance(c.get("code"), str) and GO_CASE_RE.search(c["code"])]
    elif lang == "php":
        # 白包阶段4：PHPUnit 形态（function test_*）——混写 Python/jest/go 在解析层剔除
        cleaned = [c for c in cases if isinstance(c, dict)
                   and isinstance(c.get("code"), str) and PHP_CASE_RE.search(c["code"])]
    else:
        # 用例代码两种语言形态：Python def test_… / jest test("test_…" 或 it("test_…"
        cleaned = [c for c in cases if isinstance(c, dict) and (
            (isinstance(c.get("code"), str) and (re.search(r"def\s+test_\w+", c["code"])
                                                  or JS_CASE_RE.search(c["code"])))
            or (isinstance(c.get("template"), str) and isinstance(c.get("slots"), dict)))]
    if not cleaned:
        # 顶层槽位形态容错（D8 补跑实锤）：LLM 偶尔把整个响应用 T2 槽位格式返回
        # （顶层 target + 全 value/error 行，无标准 cases 包装）——包一层成单条
        # 模板用例交渲染器展开，不再整批判 None。JS/TS 不适用（模板渲染 def test_）。
        if lang in ("javascript", "typescript"):
            return None
        if isinstance(data.get("target"), str) and cases and all(
                isinstance(c, dict) and ("value" in c or "error" in c) for c in cases):
            kind = "T1" if "classes" in data else "T2"
            func = data["target"].split("(", 1)[0].strip() or "case"
            data = {"imports": data.get("imports", ""),
                    "cases": [{"name": f"test_{func}_模板", "method": None,
                               "template": kind, "slots": dict(data)}]}
            return data
        return None
    # 字段类型归一（2026-11-15 E9 corpus-002 实锤）：LLM 偶尔把 expected/input/
    # steps 返回成数组/对象而非字符串——下游 _verify_expected_basis 的 2-gram 拼接
    # 与 test_cases 落库（TEXT 列）都会炸（TypeError/InterfaceError）。dict/list →
    # JSON 文本（保语义可读），其它非字符串 → str()。
    for c in cleaned:
        for k in ("expected", "input", "steps"):
            v = c.get(k)
            if v is None or isinstance(v, str):
                continue
            c[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    data["cases"] = cleaned
    return data


def _extract_api_docs(ws: Path, max_chars: int = 2000, lang: str | None = None) -> str | None:
    """C 源·docstring grounding（领域知识注入调研 §C）：AST 遍历取模块级+顶层函数/类的
    docstring，渲染为 "### 模块.符号" + 换行 + 原文。确定性、零 LLM；超预算截断时优先
    保留含 Args/Returns/Raises 段的（行为规范密度高）；无任何 docstring 返回 None。
    任务卡-F2 阶段 1 #5：去 Python 锁——lang="go"（或自动探测：无 .py 有 .go）走 godoc 分支。"""
    if lang is None:
        ws2 = Path(ws)
        has_py = any(p.suffix == ".py" for p in ws2.rglob("*.py")
                     if not any(part in config.SKIP_DIRS for part in p.parts))
        lang = "python" if has_py else ("go" if any(ws2.rglob("*.go")) else "python")
    if lang == "go":
        return _extract_api_docs_go(ws, max_chars=max_chars)
    docs: list[tuple[str, str, int]] = []   # (标题, 正文, 语义密度分)
    nl = chr(10)                             # 规避 heredoc 转义坑：换行一律 chr(10)
    for p in sorted(Path(ws).rglob("*.py")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        mod = p.stem
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            docs.append((f"### {mod}（模块）{nl}{mod_doc.strip()}", mod_doc, 0))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    score = sum(k in doc for k in ("Args", "Returns", "Raises", "参数", "返回", "抛出"))
                    docs.append((f"### {mod}.{node.name}{nl}{doc.strip()}", doc, score))
    if not docs:
        return None
    # 超预算：先按语义密度降序保优质，再按标题序稳定输出
    docs.sort(key=lambda d: (-d[2], d[0]))
    kept, used = [], 0
    for title, _body, _score in sorted(docs, key=lambda d: (-d[2], d[0])):
        if used + len(title) > max_chars:
            continue
        kept.append(title)
        used += len(title)
    return nl.join(kept) if kept else None


def _extract_api_docs_go(ws: Path, max_chars: int = 2000) -> str | None:
    """Go godoc 提取（任务卡-F2 阶段 1 #5，确定性零 LLM）：紧贴 func 声明的连续 //
    注释块 + 函数签名（含接收者与返回类型），渲染为 "### 包.Func" + 注释正文 + 签名。
    跳过 _test.go；无任何注释返回 None（诚实）。godoc 惯例=注释首行以被注释的
    标识符开头，此处不强制校验该惯例。"""
    import re as _re
    func_re = _re.compile(
        r"^func\s+(?:\((?P<recv>\w+\s+\*?\w+)\)\s+)?(?P<name>\w+)\s*"
        r"\((?P<params>[^)]*)\)(?P<rest>[^{]*)",
        _re.M)
    nl = chr(10)
    docs: list[tuple[str, int]] = []   # (渲染块, 语义密度分)
    for p in sorted(Path(ws).rglob("*.go")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.endswith("_test.go"):
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        pkg = ""
        for line in lines:
            if line.startswith("package "):
                pkg = line.split(None, 1)[1].strip()
                break
        text = nl.join(lines)
        for m in func_re.finditer(text):
            name = m.group("name")
            if name == "main":
                continue
            lineno = text[:m.start()].count(nl)         # func 所在行（0 基）
            comment_lines: list[str] = []
            j = lineno - 1
            while j >= 0 and lines[j].lstrip().startswith("//"):
                comment_lines.insert(0, lines[j].strip()[2:].strip())
                j -= 1
            if not comment_lines:
                continue
            recv = f"({m.group('recv')}) " if m.group("recv") else ""
            rest = (m.group("rest") or "").strip()
            sig = f"func {recv}{name}({m.group('params').strip()}) {rest}".rstrip()
            body = nl.join(comment_lines)
            score = sum(k in body for k in ("Returns", "返回", "边界", "抛出"))
            docs.append((f"### {pkg}.{name}{nl}{body}{nl}{sig}", score))
    if not docs:
        return None
    docs.sort(key=lambda d: (-d[1], d[0]))
    kept, used = [], 0
    for block, _score in sorted(docs, key=lambda d: (-d[1], d[0])):
        if used + len(block) > max_chars:
            continue
        kept.append(block)
        used += len(block)
    return nl.join(kept) if kept else None


def _extract_usage_examples(ws: Path, limit: int = 5, max_chars: int = 1600) -> str | None:
    """A 源·repo 校准（领域知识注入调研 §A，防迁就是红线）：扫仓库自带 tests/ 的**调用形态**——
    import 被测包的语句 + 测试函数内的调用表达式 + pytest.raises 的异常触发方式，**剥除断言行**。
    只示范"怎么正确调用"（参数形态/import 方式/异常触发），不是预期值来源——
    预期值由需求/docstring 决定；对齐自带测试的预期=对齐实现现状，会加剧 2412.14137 迁就。
    生成时无法预知哪些自带测试会过（要先跑一遍，成本翻倍）→ 实现口径=全部自带测试的
    调用形态+剥断言（复审修正①），剥了断言后过不过已无关紧要。无自带测试返回 None。"""
    table = _module_top_level(ws)
    nl = chr(10)
    snippets: list[str] = []
    for p in sorted(Path(ws).rglob("*.py")):
        if len(snippets) >= limit:
            break
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        # 只看仓库自带测试（被测源码已由 _collect_sources 收录，这里要的是"别人怎么调它"）；
        # Agent/人工产物（test_gen_*/test_imported/test_acceptance）不是自带锚点，排除
        if not (p.name.startswith("test_") or p.name.endswith("_test.py")):
            continue
        if p.name.startswith(("test_gen_", "test_imported", "test_acceptance")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()

        def seg(node) -> str:
            return (lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else "").strip()

        kept: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) \
                    else (node.names[0].name.split(".")[0] if node.names else "")
                if mod in table:      # 只留 import 被测包的语句（pytest/hypothesis 不是锚点）
                    kept.append(seg(node))
            elif isinstance(node, ast.FunctionDef):
                # 下钻测试函数体：调用表达式语句 + raises 上下文（异常触发方式）；
                # assert 语句天然不收（不是 Expr/With）——剥断言是防迁就切分线
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call):
                        kept.append(seg(sub))
                    elif isinstance(sub, ast.With):
                        for item in sub.items:
                            ctx = item.context_expr
                            if isinstance(ctx, ast.Call):
                                fn = ctx.func
                                name = fn.attr if isinstance(fn, ast.Attribute) \
                                    else (fn.id if isinstance(fn, ast.Name) else "")
                                if name == "raises":
                                    kept.append(seg(sub))
            if len(kept) >= 8:
                break
        kept = [s for s in kept if s and not s.startswith("assert")][:8]
        if kept:
            snippets.append(f"# {p.relative_to(ws).as_posix()}" + nl + nl.join(kept))
    text = (f"{nl}{nl}".join(snippets))[:max_chars] if snippets else None
    # kill 检查（防自欺，调研文档§每源一 kill）：残片必须不含断言——剥除断言是防迁就切分线
    if text and re.search(r"(?m)^\s*assert\b", text):
        cleaned = nl.join(l for l in text.split(nl) if not re.match(r"\s*assert\b", l))
        text = cleaned[:max_chars]
    return text or None


def _module_top_level(ws: Path) -> dict[str, set[str]]:
    """被测工作区各模块的顶层符号表（静态闸用）：module stem -> {def/class 名}。"""
    table: dict[str, set[str]] = {}
    for p in ws.rglob("*.py"):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        # 包入口以包目录名为 import 名（D3：否则 from pkg import x 的闸校验形同虚设）
        key = p.parent.name if p.stem == "__init__" else p.stem
        # Loop-10（三角色实验首批实锤）：re-export 型包的 __init__.py 全是
        # `from .sub import X` 转发（tenacity/requests 均如此），ast 里不是
        # FunctionDef——把转发名也纳入符号表，否则静态闸误杀合法 import
        if p.stem == "__init__":
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
                elif isinstance(node, ast.Assign):
                    pass  # __all__ 等赋值不展开（v2：解析 __all__ 更精确）
        table.setdefault(key, set()).update(names)
    return table


def _decorator_is_parametrize(dec: ast.expr) -> bool:
    """N1（防御矩阵 v2）：参数化装饰器判定——pytest.mark.parametrize / parameterized.expand
    及直接以 parametrize/parameterized 命名的装饰器均命中。带参装饰器是 Call 节点，先解包。"""
    if isinstance(dec, ast.Call):
        dec = dec.func
    names: list[str] = []
    node: ast.expr = dec
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return any(n in ("parametrize", "parameterized", "parameterized_class") for n in names)


def _patch_target_hits(tree: ast.AST, table: dict) -> list[str]:
    """N2（防御矩阵 v2）：mock 目标解析进「被测模块」的调用点清单。

    覆盖四形态：mock.patch("mod.f") 字符串 / patch.object(mod, "f") 对象 /
    monkeypatch.setattr(mod, "f", …) 对象 / monkeypatch.setattr("mod.f", …) 字符串。
    只有目标首段命中工作区被测模块名（table 键）才判命中——mock 外部依赖
    （requests/网络/时钟/文件系统）放行不误杀。"""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        names: list[str] = []
        while isinstance(f, ast.Attribute):
            names.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            names.append(f.id)
        base = names[0] if names else ""
        if not (base in ("patch", "setattr")
                or (base == "object" and "patch" in names[1:])):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        target = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            target = arg.value.split(".")[0]
        elif isinstance(arg, ast.Name):
            target = arg.id
        if target and target in table:
            hits.append(ast.unparse(node.func))
    return hits


JAVA_CASE_RE = re.compile(r"@\s*Test\b|void\s+test_\w+\s*\(")
# 白包阶段3：go test 用例形态（func TestXxx(t *testing.T)，表驱动/子测试均含）
GO_CASE_RE = re.compile(r"func\s+Test\w+\s*\(\s*t\s+\*testing\.T\s*\)")
# 白包阶段4：PHPUnit 用例形态（function test_*，TestCase 类内方法）
PHP_CASE_RE = re.compile(r"function\s+test\w*\s*\(")

# B2（任务卡-369 20261105）SUT 功能函数候选清单（最小版）：
# Python 用 ast 顶层函数；JS 用正则（核查时已验证可提 60+ 名）；
# 0 函数文件显式标注「纯数据文件，禁作被测单元」（369 questions.js 形态）
# 白包阶段1（TS 升 Tier2）：补 export function / export default / 方法简写形态
# （TS 最常见导出是 export function name(...)，缺它会把 TS 源全判"纯数据文件"）
_JS_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    r"|(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\s*)?\()"
    r"|(?:^|\n)\s*([A-Za-z_$][\w$]*)\s*:\s*function\s*\(")


def _sut_manifest(sources: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for fname, code in sources:
        if fname.startswith("test_") or fname.endswith(
                ("_test.py", ".test.js", "_test.js", ".test.ts", "_test.ts")):
            continue                                 # 剔除测试生成物/自带测试（jest testMatch 同口径）
        funcs: list[str] = []
        if fname.endswith(".py"):
            try:
                tree = ast.parse(code)
            except SyntaxError:
                tree = None
            if tree is not None:
                funcs = [n.name for n in tree.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            for m in _JS_FUNC_RE.finditer(code or ""):
                name = next(g for g in m.groups() if g)
                if name and name not in funcs:
                    funcs.append(name)
        if funcs:
            out.append(f"{fname}: {', '.join(funcs[:12])}"
                       + ("…" if len(funcs) > 12 else ""))
        else:
            out.append(f"{fname}（纯数据文件，无可执行函数——禁作被测单元）")
    return out


# B2+（415 实锤，2026-11-06）需求-能力错配确定性核对：需求关键词 → 能力清单零命中。
# 词表=高频双语映射（启发式，诚实边界：语义近似靠词表覆盖，漏配可能）。
# 两级匹配：prefix_ok=True 前缀族匹配（add* 算添加家族——addRecord 应命中「添加」）；
# 「注册」prefix_ok=False 全词匹配——register 前缀会撞 registerSubject（科目包注册≠
# 账户注册，415 语义冲突实锤）。token 皆有字母环视（design 不被 sign 误命中；
# snake_case 的 user_login 可命中 login——下划线不算字母）。仅当命中关键词全部
# 落空才报警，部分命中不扰（保守降噪）。
_REQ_CAP_KEYWORDS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "登录": (True, ("login", "signin", "sign", "auth", "session", "token")),
    "登陆": (True, ("login", "signin", "sign", "auth", "session", "token")),
    "注册": (False, ("register", "signup", "account", "user", "auth")),
    "删除": (True, ("delete", "remove", "destroy")),
    "添加": (True, ("add", "append", "create", "insert", "push", "save")),
    "新增": (True, ("add", "append", "create", "insert", "push", "save")),
    "搜索": (True, ("search", "query", "find", "lookup")),
    "查询": (True, ("search", "query", "find", "lookup")),
    "上传": (True, ("upload",)),
    "下载": (True, ("download", "export")),
    "导出": (True, ("export", "download")),
    "支付": (True, ("pay", "checkout")),
    "订单": (True, ("order", "cart")),
    "用户": (True, ("user", "account", "profile", "auth")),
    "密码": (True, ("password", "passwd", "pwd", "auth")),
}


def _kw_regex(prefix_ok: bool, tokens: tuple[str, ...]) -> re.Pattern:
    head = r"(?<![A-Za-z0-9])(?:" + "|".join(tokens) + r")"
    return re.compile(head + ("" if prefix_ok else r"(?![A-Za-z0-9])"))


_REQ_TOKEN_HIT_RE = {k: _kw_regex(po, ts) for k, (po, ts) in _REQ_CAP_KEYWORDS.items()}


def requirement_capability_mismatch(requirement: str,
                                    sources: list[tuple[str, str]]) -> str | None:
    """B2+（415 实锤）：需求关键词在产品能力清单零命中的确定性核对。

    返回警告文本（注入生成提示词）或 None（无错配/无关键词/无源码）。
    415 形态：需求"验证登陆注册系统是否有异常"，产品（纯前端刷题工具）无任何
    账户体系函数——生成器原本静默为不存在的功能造 11 条自 mock 用例。"""
    text = requirement or ""
    if not text or not sources:
        return None
    # 能力干草堆 = 函数名 + 文件名（auth.py/login.js 这类命名也算命中）
    hay_parts: list[str] = []
    for fname, code in sources:
        hay_parts.append(Path(fname).stem.lower())
        if fname.endswith(".py"):
            try:
                tree = ast.parse(code or "")
            except SyntaxError:
                tree = None
            if tree is not None:
                hay_parts.extend(n.name.lower() for n in tree.body
                                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        else:
            for m in _JS_FUNC_RE.finditer(code or ""):
                g = next((g for g in m.groups() if g), None)
                if g:
                    hay_parts.append(g.lower())
    haystack = " ".join(hay_parts)
    missed: list[str] = []
    for key, rx in _REQ_TOKEN_HIT_RE.items():
        if key in text and not rx.search(haystack):
            missed.append(key)
    if not missed:
        return None
    return (f"需求中的关键词「{'、'.join(missed)}」在本项目能力清单（函数/文件名）中"
            f"未找到对应实现。")


# B1（任务卡-369 20261105）JS 自导自演文本启发式（无 AST，DEFENSE_V2_ENABLED 开才生效；
# 369 实锤形态：对全大写导出数据数组重新赋值/重组 QUESTIONS = QUESTIONS.filter(...)）。
# 诚实边界：启发式有误杀/漏杀可能，拦截率由 F4 红队格度量（论文如实标注）。
_JS_REDECL_CONST_RE = re.compile(
    r"^\s*(?:const|let|var)\s+[A-Z][A-Z_0-9]{2,}\s*=\s*[^=]", re.M)
_JS_DATA_RECHAIN_RE = re.compile(
    r"^\s*[A-Z][A-Z_0-9]{2,}\s*=\s*[A-Z][A-Z_0-9]*\s*\.\s*"
    r"(?:filter|map|concat|slice|sort|reverse|splice|push|pop|shift|unshift)\s*\(", re.M)
# 369 实锤主形态（4/4 命中）：vm.runInContext("QUESTIONS = QUESTIONS.filter(...)", ctx)
# ——对导出数据的重新赋值嵌在 vm 脚本字符串里（只读如 QUESTIONS.length 不命中）
_JS_VM_REASSIGN_RE = re.compile(
    r"runInContext\s*\(\s*[\"']\s*[A-Z][A-Z_0-9]{2,}\s*=[^=]")
# 415 实锤形态（11/11 命中，2026-11-06）：完全自包含用例——login/register/sanitize 全部
# 内联定义在测试体内，require/readFileSync/vm/fetch/child_process 零命中（不碰仓库任何
# 文件），11 条"通过"全是空洞通过。产品引用通道任一命中即放行。
_JS_PRODUCT_REF_RE = re.compile(
    r"require\s*\(|readFileSync|readFile\s*\(|\bvm\s*\.|fetch\s*\(|"
    r"child_process|execSync|\bspawn\s*\(|axios|https?\s*\.\s*(?:get|post|request)\b")


def _js_selfplay_filter(cases: list[dict], imports_text: str = "") -> tuple[list[dict], list[str]]:
    """JS 自导自演检测（文本启发式）：①顶层重声明全大写导出常量 ②对全大写标识符
    挂数据重组链（filter/map/concat/...）③vm 脚本内对全大写导出标识符重新赋值
    ④零产品引用（415 形态：完全自包含的 mock 用例，被测产品零执行，"通过"=空洞
    通过）——四种形态都是"被测功能未被执行"。正常调用型用例（函数调用/断言
    返回值）不受影响。imports_text=脚本级共享 imports（419 实锤：require 写在
    imports 字段而非用例体，零引用判定必须合并扫描，否则误杀真实触达产品的用例）。"""
    kept, rejected = [], []
    for c in cases:
        code = c.get("code") or ""
        full = code + "\n" + (imports_text or "")
        why = None
        if _JS_REDECL_CONST_RE.search(code):
            why = "顶层重声明全大写导出常量（疑似重组被测数据而非调用被测功能）"
        elif _JS_DATA_RECHAIN_RE.search(code):
            why = "对全大写导出标识符挂数据重组链（自导自演：被测功能未执行）"
        elif _JS_VM_REASSIGN_RE.search(code):
            why = "vm 脚本内对全大写导出标识符重新赋值（369 实锤形态：改 const 数据" \
                  "必然 TypeError 且被测功能未执行）"
        elif not _JS_PRODUCT_REF_RE.search(full):
            why = "自包含用例（415 实锤形态：未引用任何产品文件/模块，login/register 等" \
                  "全部内联定义在测试体内——测试对象是用例自己的参考实现，被测产品零执行）"
        if why:
            rejected.append(f"{c.get('name')}: {why}")
        else:
            kept.append(c)
    return kept, rejected


_SUT_MUTATION_METHODS = {"clear", "append", "extend", "insert", "pop", "popitem",
                         "remove", "update", "sort", "reverse", "setdefault"}


def _sut_root(node: ast.AST, table: dict) -> str | None:
    """属性链根是被测模块名 → 返回模块名（mymod.DATA.clear() / mymod.Q = x 的根都是 mymod）。"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id in table:
        return cur.id
    return None


def _mutates_sut_state(tree: "ast.Module", table: dict) -> list[str]:
    """F4 红队发现（2026-11-06）：B1 只挂 JS 分支——Python 侧「对被测模块状态直接
    赋值/变异」是自导自演的 Python 形态，静态闸原本拦不住。两形态：①赋值/删除
    （mymod.QUESTIONS = ... / del mymod.Q）②变异方法调用（mymod.DATA.clear()——
    F4 首轮实锤漏过的方法形态）。与 N2（mock 被测模块）同族。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, ast.Attribute) and _sut_root(tgt, table):
                    hits.append(ast.unparse(tgt)[:60])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr in _SUT_MUTATION_METHODS
                    and _sut_root(node.func.value, table)):
                hits.append(ast.unparse(node)[:60])
    return hits


def _static_filter(cases: list[dict], ws: Path, imports_text: str = "") -> tuple[list[dict], list[str]]:
    """生成后静态闸（§3.6 第 1 道，确定性、零成本）：
    语法可解析 + from-import 的符号在被测模块顶层存在。剔除理由如实返回。
    JS/TS 分支：Python AST 不适用——旗标关时跳过静态闸（JS 的安全闸=容器内
    node --check 语法检查，在收集闸步骤执行），理由如实标注；
    DEFENSE_V2_ENABLED 开=B1 文本启发式闸（JS 自导自演检测，任务卡-369 B1；
    imports_text=脚本级共享 imports，零产品引用判定必须合并扫描——419 实锤）。"""
    if cases and any(JS_CASE_RE.search((c.get("code") or "")) for c in cases):
        if config.DEFENSE_V2_ENABLED:
            return _js_selfplay_filter(cases, imports_text)
        return cases, []   # JS 形态：闸移交收集步骤（node --check），这里不做 AST 审计
    if cases and any(JAVA_CASE_RE.search((c.get("code") or "")) for c in cases):
        # 白包阶段2：JUnit5 形态——Python AST 不适用；闸=容器内 mvn test-compile（收集闸步骤）
        return cases, []
    if cases and any(GO_CASE_RE.search((c.get("code") or "")) for c in cases):
        # 白包阶段3：go test 形态——Python AST 不适用；闸=容器内 go build+go test -c（收集闸步骤）
        return cases, []
    if cases and any(PHP_CASE_RE.search((c.get("code") or "")) for c in cases):
        # 白包阶段4：PHPUnit 形态——Python AST 不适用；闸=容器内 php -l（收集闸步骤）
        return cases, []
    table = _module_top_level(ws)
    kept, rejected = [], []
    for c in cases:
        try:
            tree = ast.parse(c["code"])
        except SyntaxError as e:
            rejected.append(f"{c.get('name')}: 语法错误({e.lineno}行)")
            continue
        ok = True
        product_imported = False
        for node in ast.walk(tree):
            # P1-4a：import 白名单审计（Import 与 ImportFrom 都查）——提示注入防线，
            # 白名单外（os/subprocess/socket/requests...）整案剔除并留理由
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod in table:
                        product_imported = True
                    if mod not in IMPORT_WHITELIST and mod not in table:
                        rejected.append(f"{c.get('name')}: import {alias.name} 不在白名单"
                                        f"（测试框架/标准库无害子集/被测模块之外不执行）")
                        ok = False
                        break
            if not ok:
                break
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            mod = node.module.split(".")[0]
            if mod in table:
                product_imported = True
            if mod not in IMPORT_WHITELIST and mod not in table:
                rejected.append(f"{c.get('name')}: from {node.module} import … 不在白名单"
                                f"（测试框架/标准库无害子集/被测模块之外不执行）")
                ok = False
                break
            if mod in table:
                for alias in node.names:
                    if alias.name != "*" and alias.name not in table[mod]:
                        rejected.append(f"{c.get('name')}: from {mod} import {alias.name} 不存在")
                        ok = False
                        break
            if not ok:
                break
        # N1（防御矩阵 v2）：参数化装饰器整案剔除（闸层兜底，提示词第 6 条的确定性对偶）
        if ok and config.DEFENSE_V2_ENABLED:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(_decorator_is_parametrize(d) for d in node.decorator_list):
                        rejected.append(f"{c.get('name')}: 参数化装饰器（单条用例=单个测试函数，"
                                        f"parametrize 会被整案剔除）")
                        ok = False
                        break
        # N2（防御矩阵 v2）：mock 被测模块整案剔除（mock 被测单元=掩盖真实行为，无证伪能力；
        # mock 外部依赖放行）
        if ok and config.DEFENSE_V2_ENABLED:
            hits = _patch_target_hits(tree, table)
            if hits:
                rejected.append(f"{c.get('name')}: mock 被测模块（{'/'.join(hits[:2])}——"
                                f"仅允许 mock 网络/文件系统/时钟等外部依赖）")
                ok = False
        # F4 红队补闸：Python 自导自演（对被测模块状态直接赋值/变异）
        if ok and config.DEFENSE_V2_ENABLED:
            hits = _mutates_sut_state(tree, table)
            if hits:
                rejected.append(f"{c.get('name')}: 直接改写被测模块状态（{hits[0]}——"
                                f"自导自演：改完自己断言，被测功能未执行）")
                ok = False
        # 415 补闸（2026-11-06）：Python 自包含用例——无任何指向被测模块的 import，
        # 测试对象是用例内联实现（JS 侧零产品引用规则的对偶；table 空=无从判定，跳过）
        if ok and config.DEFENSE_V2_ENABLED and table and not product_imported:
            rejected.append(f"{c.get('name')}: 自包含用例（无任何指向被测模块的 import——"
                            f"测试对象是用例自己的内联实现，被测产品零执行）")
            ok = False
        if ok:
            kept.append(c)
    return kept, rejected


def _audit_one_assert(test: ast.expr) -> tuple[bool, str | None]:
    """单条断言的强弱判定：(是否强, 弱原因)。强 = 具体值/关系 oracle。

    BoolOp 联接（assert a==1 and b==2）逐臂审计：全部臂强才判强——
    整体落"裸真值"会把两臂皆具体比较的好断言误杀（module_check FINDING）。
    """
    if isinstance(test, ast.BoolOp):
        for i, arm in enumerate(test.values, 1):
            ok, why = _audit_one_assert(arm)      # 递归：嵌套联接同样逐臂
            if not ok:
                return False, f"联接第{i}臂弱({why})"
        return True, None
    if isinstance(test, ast.Compare):
        comps = [test.left] + list(test.comparators)
        has_rel = any(not isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops)
        if has_rel:
            return True, None                       # ==/!=/</>/in 等值或序关系
        none_check = any(isinstance(c, ast.Constant) and c.value is None for c in comps)
        bool_check = any(isinstance(c, ast.Constant) and isinstance(c.value, bool)
                         for c in comps)
        if bool_check:
            return True, None                       # is True/False：显式布尔 oracle
        if none_check:
            return False, "is (not) None 无具体预期值"
        return True, None
    if isinstance(test, ast.Call):
        fn = test.func
        name = fn.attr if isinstance(fn, ast.Attribute) else \
            (fn.id if isinstance(fn, ast.Name) else "")
        if name == "raises":
            return True, None                       # 异常类型 oracle
        return False, f"裸调用真值断言({name or 'expr'})"
    return False, "裸真值/常量断言"


def assertion_audit(code: str) -> dict:
    """断言质量审计（纯函数，方案A 的度量与闸共用）：统计用例内强/弱断言。

    强 = 具体关系比较（==/!=/序关系/in）、is True/False、pytest.raises(异常类型)、
    联接断言逐臂皆强（assert a==1 and b==2）；
    弱 = 裸真值断言、is (not) None、isinstance、无断言、联接断言含弱臂 —— 不具备证伪具体预期值的能力
    （动机：2412.14137 实证生成器会写"为通过而设计"的断言；UW ASE'25 oracle 实证）。
    返回 {total, strong, weak, reasons}；无断言整案计弱。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["语法不可解析"]}
    strong, weak, reasons = 0, 0, []
    # raises 上下文管理器（with pytest.raises(...)）也是显式 oracle，与 assert 同权统计
    raises_calls = []
    for w in ast.walk(tree):
        if isinstance(w, ast.With):
            for item in w.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else \
                        (fn.id if isinstance(fn, ast.Name) else "")
                    if name == "raises":
                        raises_calls.append(ctx)
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    if not asserts and not raises_calls:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["无断言（无 oracle）"]}
    for a in asserts:
        ok, why = _audit_one_assert(a.test)
        if ok:
            strong += 1
        else:
            weak += 1
            reasons.append(why)
    strong += len(raises_calls)
    return {"total": strong + weak, "strong": strong, "weak": weak, "reasons": reasons}


def _verify_expected_basis(cases: list[dict], requirement: str) -> list[str]:
    """D8（2026-10-31 真机实锤）：LLM 会把推断预期洗成「需求明示」（expected_basis 假标注）——
    归因语义信任该字段，假标注即假 product_defect（titlecase 任务160：需求未提「AND 转小写」
    却标注需求明示）。确定性校验：预期文本与需求 2-gram 零重叠的「需求明示」降级「模型推断」。
    诚实边界：需求提及主题但规则理解错（缩写词规则优先于小词规则）无法用重叠判定，
    仍靠执行暴露——oracle 问题本质，论文讨论素材。只校验 code 形态用例（模板槽位的
    expected_basis 由渲染器语义负责）。"""
    from .evaluate import _grams
    req = _grams(requirement or "")
    downgraded: list[str] = []

    def _as_text(v) -> str:
        # 字段类型防御（2026-11-15 E9 corpus-002 实锤：expected 返回数组→list+str 炸）；
        # 解析边界已归一，此处兜底其它直呼路径
        if v is None or isinstance(v, str):
            return v or ""
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)

    for c in cases:
        if c.get("template"):
            continue
        if (c.get("expected_basis") or "").strip() == "需求明示":
            eg = _grams(_as_text(c.get("expected")) + _as_text(c.get("input")))
            if req and eg and not (eg & req):
                c["expected_basis"] = "模型推断"
                downgraded.append(c.get("name") or "?")
    return downgraded


def _condition_gaps(conditions: list[dict], cases: list[dict]) -> list[dict]:
    """方案C：确定性 2-gram 映射（与 map_requirements 启发式同源）找出无用例对准的条件。"""
    from .evaluate import _heuristic_map          # 延迟导入避免环形依赖
    pseudo = [{"id": i + 1, "condition": c["condition"]} for i, c in enumerate(conditions)]
    rows = [{"name": c["name"], "expected": c.get("expected") or "",
             "content": c.get("content") or c.get("code") or ""} for c in cases]
    mapping = _heuristic_map(pseudo, rows)
    return [c for c, p in zip(conditions, pseudo) if not mapping.get(p["id"])]


def _rebuild_imported_file(conn, tid: int, ws: Path) -> str:
    """以库为准重建人工/AI 插入用例文件（仅 enabled=1），随下次 provision 快照进容器。
    文件名按项目语言分流（晚间遗留实锤：JS 项目写 test_imported.py jest 不收集）——
    JS → test_imported.test.js（jest testMatch 收集 + // 注释头）；其余 → test_imported.py。"""
    imported = conn.execute(
        """SELECT c.content FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.source='imported' AND c.enabled=1 ORDER BY c.id""", (tid,)).fetchall()
    lang_row = None
    try:
        lang_row = conn.execute(
            """SELECT p.primary_language FROM test_tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.id=?""", (tid,)).fetchone()
    except Exception:  # noqa: BLE001 —— 轻量测试环境可能无 tasks/projects 表：默认 python 命名
        pass
    lang_val = lang_row["primary_language"] if lang_row else "python"
    is_js = lang_val in ("javascript", "typescript")   # TS 同 jest 收集（白包阶段1）
    ext = ".test.ts" if lang_val == "typescript" else (".test.js" if is_js else ".py")
    fname = f"test_imported{ext}"
    header = ("// 人工/AI 插入用例（test_cases.source=imported，enabled=1）" if is_js
              else '"""人工/AI 插入用例（test_cases.source=imported，enabled=1）"""')
    content = header + "\n\n" + "\n\n".join(r["content"].strip() for r in imported) + "\n"
    # 清掉另一种语言的旧文件（语言判定不随任务重跑变化，防御性清理残留）
    for stale in ("test_imported.py", "test_imported.test.js", "test_imported.test.ts"):
        if stale != fname:
            (Path(ws) / stale).unlink(missing_ok=True)
    (Path(ws) / fname).write_text(content, encoding="utf-8")
    return content


def prune_failing_cases(ws: Path, pairs: list[tuple[str, str]],
                        task_id: int, conn) -> dict:
    """②定向修复（TestGen-LLM 准入思想）：上一轮失败用例按归因分流——

    - 用例自身坏掉的强信号（ImportError/SyntaxError/AttributeError 等）→ 剪除：
      生成文件走 AST 摘除函数，imported 文件以库为准重建（enabled=0 保留审计记录）；
    - AssertionError（疑似产品缺陷）→ 一律保留为证人（§7.2 防造假铁律）；
    - 仓库自带测试永远不动（那是被测项目的一部分，不是 Agent 的产物）。
    """
    pruned, kept = [], []
    for nodeid, err in pairs:
        fname, _, func = nodeid.partition("::")
        agent_file = fname.startswith(("test_gen_r", "test_imported"))
        if not agent_file or err not in CASE_OWN_DEFECT_ERRORS:
            kept.append({"nodeid": nodeid, "err": err or "unknown"})
            continue
        # DB 记录保留但停用（论文审计台账），文件层面真正摘除
        conn.execute(
            """UPDATE test_cases SET enabled=0 WHERE name=? AND enabled=1 AND script_id IN
               (SELECT id FROM test_scripts WHERE task_id=?)""", (func, task_id))
        conn.commit()
        path = Path(ws) / fname
        if fname == "test_imported.py":
            _rebuild_imported_file(conn, task_id, ws)
        elif path.exists():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                tree.body = [n for n in tree.body
                             if not (isinstance(n, ast.FunctionDef) and n.name == func)]
                path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
            except SyntaxError:
                path.unlink(missing_ok=True)  # 整文件已不可解析，整体废案
        pruned.append({"nodeid": nodeid, "err": err})
    return {"pruned": pruned, "kept": kept}


def refill_condition_gaps(llm, sources: list[tuple[str, str]], requirement: str,
                          conditions: list[dict], cases: list[dict], ws: Path,
                          used_names: set[str], include_methods: bool, pbt: bool,
                          task_id: int | None = None, lang: str | None = None
                          ) -> tuple[list[dict], str, dict]:
    """方案C 核心（llm 注入可单测）：查条件缺口 → 定向补生成恰 1 次 → 返回 (新用例, imports, 信息)。"""
    gaps = _condition_gaps(conditions, cases)
    info: dict = {"uncovered_before": len(gaps), "refill_generated": 0,
                  "uncovered_after": len(gaps)}
    if not gaps:
        return [], "", info
    resp = llm.invoke([("system", system_prompt_with_isolation(SYSTEM_PROMPT)),
                       ("user", build_user_prompt(
                           sources, requirement, None, None, gaps,
                           include_methods=include_methods, pbt=pbt, cond_forced=True,
                           templates=False,   # 补生成轮保持自由通道（定向补缺不做结构先验）
                           existing_cases=[c["name"] for c in cases]))])
    log_llm_call(task_id, "refill", resp,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
    data = _parse_cases(resp.content, lang=lang)
    if not data:
        info["note"] = "补生成轮 LLM 输出不可解析（诚实放弃）"
        return [], "", info
    data["cases"], rejected = _static_filter(data["cases"], ws,
                                             data.get("imports") or "")
    info["static_rejected"] = rejected
    out = []
    root_fixed = 0
    for c in data["cases"]:
        def_name = _sanitize_name(c.get("name"), len(used_names), used_names)
        code = c["code"].strip()
        code, n_fixed = _fix_js_scaffold_paths(code)   # JS 脚手架修正（Python 代码无此形态=无操作）
        root_fixed += n_fixed
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name
        out.append(c)
    if root_fixed:
        info["root_fixed"] = root_fixed
    info["refill_generated"] = len(out)
    info["uncovered_after"] = len(_condition_gaps(conditions, cases + out))
    return out, (data.get("imports") or "").strip(), info


def apply_assertion_gate(cases: list[dict], gate: bool = False
                         ) -> tuple[list[dict], list[str], dict]:
    """方案A 核心（纯函数）：断言质量审计恒算；gate=True 时剔除全弱断言用例（无证伪能力）。
    返回 (保留用例, 剔除理由列表, 统计)。混合断言（含 ≥1 强）的用例保留。"""
    audits = [assertion_audit(c["code"]) for c in cases]
    stats = {"cases": len(audits),
             "weak_cases": sum(1 for a in audits if a["strong"] == 0),
             "assert_total": sum(a["total"] for a in audits),
             "assert_weak": sum(a["weak"] for a in audits)}
    rejected: list[str] = []
    if not gate:
        return cases, rejected, stats
    kept = []
    for c, a in zip(cases, audits):
        if a["strong"] == 0:
            rejected.append(f"{c.get('name')}: {a['reasons'][0]}")
        else:
            kept.append(c)
    stats["gate_rejected"] = len(rejected)
    return kept, rejected, stats


def parse_cross_review(text: str) -> dict[str, dict] | None:
    """从审查 LLM 回复鲁棒提取 {name: {verdict, reason}}；不可用返回 None（fail-open）。

    只认 verdict∈{pass,fail} 且 name 为字符串的条目；一条有效判决都没有 = 没审，
    同样返回 None 交由调用方放行（诚实降级，不硬造判决）。
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        return None
    out: dict[str, dict] = {}
    for r in reviews:
        if (isinstance(r, dict) and isinstance(r.get("name"), str)
                and r.get("verdict") in ("pass", "fail")):
            out[r["name"]] = {"verdict": r["verdict"],
                              "reason": str(r.get("reason") or "")[:200]}
    return out or None


def cross_review_gate(llm, cases: list[dict], requirement: str,
                      sources: list[tuple[str, str]], ws: Path,
                      used_names: set[str], task_id: int | None = None,
                      lang: str | None = None) -> tuple[list[dict], dict]:
    """§3.6 第 3 道闸：LLM 交叉语义审（V9，审查者角色，≠生成器自审走过场）。

    逐案审断言是否对应需求；fail 用例**单独重造 1 次**（受控，不无限循环）→ 重造产物
    复审仍 fail 则剔除（单案处置不拖累整批——这正是与"整批废案"的可收集闸的区别）。
    - 审查输出不可解析 → fail-open 全放行（辅助闸不因自身故障废掉整批生成，
      与 review_report 失败放行同语义）；复审不可解析同样放行。
    - 无需求文本 → 跳过（没有对齐基准，无从审起）。
    返回 (用例清单, 台账信息)；LLM 调用逐次记 llm_calls（phase=cross_review/regen_case）。
    """
    info: dict = {"reviewed": len(cases), "failed": [], "regenerated": 0,
                  "dropped": [], "note": ""}
    if not cases or not (requirement or "").strip():
        info["note"] = "无用例或无需求文本，跳过交叉审（无对齐基准）"
        return cases, info
    resp = llm.invoke([("system", CROSS_REVIEW_SYSTEM),
                       ("user", build_cross_review_prompt(requirement, cases))])
    log_llm_call(task_id, "cross_review", resp,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
    verdicts = parse_cross_review(resp.content)
    if verdicts is None:
        info["note"] = "审查输出不可解析（fail-open 放行，留痕备查）"
        return cases, info
    failed = [c for c in cases if verdicts.get(c["name"], {}).get("verdict") == "fail"]
    info["failed"] = [{"name": c["name"], "reason": verdicts[c["name"]]["reason"]}
                      for c in failed]
    kept = [c for c in cases if verdicts.get(c["name"], {}).get("verdict") != "fail"]
    if not failed:
        return cases, info
    # 单独重造：逐案定向重造（原案被否决的理由随提示词进入），产物过静态闸 + def 名对齐
    regen: list[dict] = []
    for i, c in enumerate(failed):
        r2 = llm.invoke([("system", system_prompt_with_isolation(SYSTEM_PROMPT)),
                         ("user", build_regen_prompt(sources, requirement, c,
                                                     verdicts[c["name"]]["reason"]))])
        log_llm_call(task_id, "regen_case", r2,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
        data = _parse_cases(r2.content, lang=lang)
        new: dict | None = None
        if data and data["cases"]:
            cand, _ = _static_filter(data["cases"], ws, data.get("imports") or "")
            if cand:
                one = cand[0]                     # 单案重造只取第一条
                def_name = _sanitize_name(one.get("name"), len(used_names), used_names)
                code = one["code"].strip()
                m = re.search(r"def\s+\w+", code)
                if m:
                    code = code[:m.start()] + f"def {def_name}" + code[m.end():]
                one["code"], one["name"] = code, def_name
                new = one
        if new:
            regen.append(new)
            info["regenerated"] += 1
            info["failed"][i]["regen_as"] = new["name"]   # 台账可追溯：原案 → 重造案名
        else:
            info["dropped"].append({"name": c["name"], "reason": "重造失败（输出不可用或未过静态闸）"})
    # 复审（受控 1 次）：仍 fail 剔除——迁就性断言宁缺毋滥；不可解析 fail-open 放行
    if regen:
        resp3 = llm.invoke([("system", CROSS_REVIEW_SYSTEM),
                            ("user", build_cross_review_prompt(requirement, regen))])
        log_llm_call(task_id, "cross_review", resp3,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
        v3 = parse_cross_review(resp3.content)
        survived = []
        for c in regen:
            if v3 is not None and v3.get(c["name"], {}).get("verdict") == "fail":
                info["dropped"].append({"name": c["name"], "reason": v3[c["name"]]["reason"]})
            else:
                survived.append(c)
        regen = survived
    out = kept + regen
    info["passed_cases"] = len(out)
    return out, info


SEED_SYSTEM = (
    "你是测试工程师。生成 1 条最简 pytest 用例（结构样板）：单测试函数、单断言、"
    "覆盖需求中最简单的一条行为。禁止参数化装饰器、fixture、辅助函数。"
    '只输出 JSON：{"imports": "...", "code": "def test_xxx():\\n    ..."}。'
)


def _generate_seed(llm, ws: Path, requirement: str, runner, container, fname: str,
                   tries: int = 3):
    """种子自举阶段（docs/种子自举模板设计-20261105）：生成 1 条最简用例 → 容器 collect
    验证 → 通过则返回种子全文（作为批量生成的结构锚点）。断言失败不算模板失败
    （collect 过=结构成立）；收集失败带原因重试 ≤tries 次；仍失败 → None（诚实降级，
    批量生成不带锚点照常进行）。种子文件验证后删除（结构已提取，不留执行干扰）。"""
    for attempt in range(1, tries + 1):
        try:
            src_pairs, _ = _collect_sources_detailed(ws, "python")
            src_text = "\n".join(f"### {p}\n```python\n{c}\n```" for p, c in src_pairs)
            resp = llm.invoke([("system", SEED_SYSTEM),
                               ("user", f"## 需求\n{requirement or '（按源码行为设计）'}\n\n"
                                        f"## 被测源码\n{src_text or '（无）'}")])
        except Exception as e:  # noqa: BLE001
            log.warning("种子生成 attempt=%s LLM 失败: %s", attempt, e)
            continue
        # 种子格式是单层 {imports, code}，不走 _parse_cases（它只认 cases 数组形态）
        m = re.search(r"\{.*\}", resp.content, re.S)
        if not m:
            log.warning("种子生成 attempt=%s 无 JSON", attempt)
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            try:
                data = _loads_tolerant(m.group(0))   # 模型漂移字面量容错（同批量通道）
            except (json.JSONDecodeError, ValueError):
                log.warning("种子生成 attempt=%s 输出不可解析", attempt)
                continue
        code = (data.get("code") or "").strip()
        imports = (data.get("imports") or "").strip()
        if not code or "def test_" not in code:
            log.warning("种子生成 attempt=%s 缺有效 code", attempt)
            continue
        content = ('"""结构种子（seed bootstrap，验证后删除）"""\n'
                   + (imports + "\n\n" if imports else "") + code + "\n")
        (ws / fname).write_text(content, encoding="utf-8")
        try:
            runner.copy_dir_into(container, ws)   # 种子回灌容器（collect 在容器内执行）
        except Exception as e:  # noqa: BLE001
            log.warning("种子回灌失败: %s", e)
            continue
        r = runner.exec_in(container, ["pytest", "--collect-only", "-q", fname],
                           timeout_s=120)
        if r.exit_code == 0:
            (ws / fname).unlink(missing_ok=True)   # 结构已提取，删除防执行干扰
            return content
        log.warning("种子 collect 失败 attempt=%s: %s", attempt, (r.stdout + r.stderr)[-200:])
        (ws / fname).unlink(missing_ok=True)
    return None


def generate_tests(api_key: str, args: dict, task_id: int, runner) -> str:
    """生成/增补共用管线（三道闸同源保证）：
    - 常规轮：args.round_no → test_gen_r{N}.py（version=N）
    - 对话增补轮（两阶段评审/AI 代审 regenerate）：args.feedback + args.fb_no →
      test_gen_fb{N}.py（version=调用方给定，独立文件不动已批准用例），phase=gen_feedback
    """
    ws = Path(args["workspace"])
    feedback = (args.get("feedback") or "").strip()
    fb_no = args.get("fb_no")
    phase = "gen_feedback" if feedback else "gen_tests"
    lang = (args.get("module_hint") or "python").lower()
    is_js = lang in ("javascript", "typescript")   # JS/TS 执行链路 Tier2（白包阶段1 升 TS）：
    # jest 形态生成；收集闸 js=node --check / ts=tsc --noEmit（collect_gate_cmd 分流）
    is_java = lang == "java"                       # 白包阶段2：JUnit5 形态生成；闸=mvn test-compile
    is_go = lang == "go"                           # 白包阶段3：go test 形态生成；闸=go build+go test -c
    is_php = lang == "php"                       # 白包阶段4：PHPUnit 形态生成；闸=php -l
    sources, source_skipped = _collect_sources_detailed(
        ws, lang, scope_paths=args.get("index_scope_paths") or None)
    # A3（2026-11-20）：零收录回退在 _collect_sources_detailed 内完成（回退台账带
    # [index-scope] 注记）——此处只检测注记打标，summary 供 nodes.generate 清空路由锁定
    scope_fallback = any(s.startswith("[index-scope]") for s in source_skipped)
    if scope_fallback:
        log.warning("gen_tests task=%s scope 零收录回退全库: scope=%s",
                    task_id, args.get("index_scope_paths"))
    if source_skipped:
        # 诚实呈现能力边界：哪些代码没进 LLM 视野（旧版静默丢弃是 H1 假缺陷根因之一）
        log.warning("gen_tests task=%s 源码预算截断 %d 段: %s",
                    task_id, len(source_skipped), "; ".join(source_skipped)[:300])
    if not sources:
        return json.dumps({"generated": 0, "reason": "未发现可测源码",
                           "source_skipped": source_skipped}, ensure_ascii=False)

    conn = get_conn()
    conditions = [dict(r) for r in conn.execute(
        "SELECT condition, priority, method_hint FROM test_conditions "
        "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()] or None
    # 条件超限降采样（E2E 三跑实锤：43 条条件→40+ 用例 JSON 超 DeepSeek 输出上限截断不可解析，
    # 20 条则稳定成功）：长度爆炸在源头治，不指望解析器救截断。缺口条数如实留痕（摘要可查）。
    # BREADTH_SAMPLING_ENABLED 开时为「15 优先级 + ≤5 函数域分散补充」（详见 _downsample_conditions）。
    if conditions:
        conditions, conditions_truncated = _downsample_conditions(
            conditions, ws, lang, task_id)
    else:
        conditions_truncated = 0

    # —— 三源 grounding 注入（各自独立开关，默认关=提示词逐字节不变） ——
    api_docs = _extract_api_docs(ws, lang=lang) if config.DOCSTRING_GROUNDING_ENABLED else None
    usage_examples = _extract_usage_examples(ws) if config.USAGE_EXAMPLES_ENABLED else None

    # max_tokens 显式给满（2026-11-17 卡 2，task1278 实锤：不传 → DeepSeek 默认 ≈4K →
    # Java 冗长用例 25-30KB 截断 → 0 生成；8192=deepseek-chat 硬上限）
    llm = make_chat(phase="gen_tests", api_key=api_key, temperature=0.2, timeout=90,
                    max_retries=2, max_tokens=config.LLM_GEN_MAX_TOKENS)
    seed_code = None
    if (config.SEED_BOOTSTRAP_ENABLED and not feedback and lang == "python"
            and runner is not None and args.get("container_name")):
        try:
            container = runner.client.containers.get(args["container_name"])
            seed_code = _generate_seed(llm, ws, args.get("requirement", ""), runner,
                                       container, "test_seed_probe.py")
            if seed_code:
                log.info("gen_tests task=%s 种子自举成功（结构锚点就位）", task_id)
        except Exception as e:  # noqa: BLE001 —— 种子失败 fail-open，批量照常
            log.warning("种子自举失败（fail-open）: %s", e)

    sut_manifest = (_sut_manifest(sources)
                    if config.SUT_MANIFEST_ENABLED and not feedback else None)
    if sut_manifest:
        log.info("gen_tests task=%s SUT 清单注入 %d 项（B2）", task_id, len(sut_manifest))
    # B2+（415 实锤）：需求-能力错配核对——需求关键词在清单零命中时注入显式警告，
    # 禁止为不存在的功能编造 mock（415 的 11 条空洞通过用例即此形态）
    sut_mismatch = (requirement_capability_mismatch(args.get("requirement", ""), sources)
                    if config.SUT_MANIFEST_ENABLED and not feedback else None)
    if sut_mismatch:
        log.warning("gen_tests task=%s 需求-能力错配：%s", task_id, sut_mismatch)
    # AL-7（2026-11-26 设计 F-AL7，Q4 范围外 F1）：targets 由此真实消费——
    # 优先策略产物的 args_hint.targets，回退显式 args.targets；注入"定向补测目标"段。
    strat = (args.get("agent_strategy") or {})
    gen_targets = (args.get("targets") or [])
    if not gen_targets:
        for p in (strat.get("priorities") or []):
            ah = p.get("args_hint") or {}
            if isinstance(ah, dict) and ah.get("targets"):
                gen_targets = list(ah["targets"])
                break
    if gen_targets:
        log.info("gen_tests task=%s 定向补测目标 %d 项（AL-7）", task_id, len(gen_targets))
    resp = llm.invoke([("system", system_prompt_with_isolation(SYSTEM_PROMPT)),
                       ("user", build_user_prompt(sources, args.get("requirement", ""),
                                                  args.get("coverage_gaps"),
                                                  args.get("coverage_target"),
                                                  conditions,
                                                  include_methods=config.GEN_METHODS_ENABLED,
                                                  boundary_attack=config.GEN_BOUNDARY_ATTACK,
                                                  pbt=config.PBT_ENABLED,
                                                  seed_example=seed_code,
                                                  templates=config.TEMPLATES_ENABLED,
                                                  state_transition=config.STATE_TRANSITION_ENABLED,
                                                  metamorphic=config.METAMORPHIC_ENABLED,
                                                  api_docs=api_docs,
                                                  usage_examples=usage_examples,
                                                  feedback_text=feedback or None,
                                                  pitfalls=config.PITFALL_KB_ENABLED,
                                                  trapkb_extra=_load_trapkb_v2(),
                                                  module_lang=args.get("module_hint", ""),
                                                  target_language=lang,
                                                  existing_cases=(args.get("existing_names") or None)
                                                  if feedback else None,
                                                  defense_v2=config.DEFENSE_V2_ENABLED,
                                                  layer_aware=config.LAYER_AWARE_ENABLED,
                                                  sut_functions=sut_manifest,
                                                  sut_mismatch=sut_mismatch,
                                                  endpoint_gaps=(args.get("endpoint_gaps") or None),
                                                  agent_diagnosis=(args.get("agent_diagnosis") or None),
                                                  agent_strategy=(args.get("agent_strategy") or None),
                                                  gen_targets=(gen_targets or None),
                                                  ctx_budget=getattr(llm, "_ctx_budget", None),
                                                  **_repo_probe_payload(ws)))])
    log_llm_call(task_id, phase, resp,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
    data = _parse_cases(resp.content, lang=lang)
    if data is None:
        # 可观测性（批4 12/12 全降级实锤的教训）：解析失败必须留原始输出，否则
        # "诚实降级"了却永远查不了因——证据蒸发与 task_366 stderr 丢弃同型
        import time as _t
        tail = (resp.content or "")[-500:]
        log.warning("gen_tests task=%s LLM 输出不可解析（%d 字符），尾部：%s",
                    task_id, len(resp.content or ""), tail[:200].replace("\n", " ⏎ "))
        fail_dir = config.DATA_DIR / "reports" / "llm_parse_fail"
        fail_dir.mkdir(parents=True, exist_ok=True)
        (fail_dir / f"task{task_id}_{phase}_{_t.strftime('%H%M%S')}.txt").write_text(
            resp.content or "", encoding="utf-8")
        return json.dumps({"generated": 0,
                           "reason": "LLM 返回无法解析为用例（诚实降级，本轮不生成）；"
                                     f"原始输出已留档 llm_parse_fail/task{task_id}/，"
                                     f"尾部：{tail[:100]}"},
                          ensure_ascii=False)

    # —— 槽位模板展开（双通道）：基础方法 T1/T2/T5 渲染成代码，失败案留台账理由剔除 ——
    data["cases"], template_rejected = expand_template_cases(data["cases"])
    if template_rejected:
        log.info("gen_tests task=%s 模板展开剔除: %s", task_id, template_rejected)
    if not data["cases"] and template_rejected:
        return json.dumps({"generated": 0, "reason": "全部用例模板展开失败（槽位无效，诚实降级）",
                           "template_rejected": template_rejected}, ensure_ascii=False)

    # —— D8 期望依据校验：无需求依据的「需求明示」降级「模型推断」（假标注→假 product_defect 根因） ——
    basis_downgraded = _verify_expected_basis(data["cases"], args.get("requirement", ""))
    if basis_downgraded:
        log.info("gen_tests task=%s 需求明示降级 %d 案: %s",
                 task_id, len(basis_downgraded), basis_downgraded[:5])

    # —— 静态闸（§3.6 第 1 道）：语法 + from-import 符号存在性，坏案在写盘前剔除 ——
    data["cases"], rejected = _static_filter(data["cases"], ws, data.get("imports") or "")
    if not data["cases"]:
        return json.dumps({"generated": 0, "reason": "全部用例未过静态审查（诚实降级）",
                           "rejected": rejected, "template_rejected": template_rejected},
                          ensure_ascii=False)

    # —— def 名对齐（A5）：用规整后的 name 重写首个 def，nodeid 从此可反查用例 ——
    # 对话增补轮：防撞池=任务全域已有用例名（增补文件与已批准文件同名函数会让归因反查失准）
    # JS 分支：重写 test("…")/it("…") 的名字参数（jest nodeid=文件::用例名，与 Python 同口径）
    used_names: set[str] = set(args.get("existing_names") or [])
    for i, c in enumerate(data["cases"]):
        def_name = _sanitize_name(c.get("name"), i, used_names)
        code = c["code"].strip()
        if is_js:
            code = re.sub(r'(\b(?:test|it)\s*\(\s*)([\'"])[^\'"]*\2',
                          lambda m: f'{m.group(1)}"{def_name}"', code, count=1)
        elif is_java:
            # JUnit5 方法名重写：首个 void test_xxx( 对齐规整名（surefire nodeid=类::方法，同口径）
            code = re.sub(r"\bvoid\s+test_\w+\s*\(", f"void {def_name}(", code, count=1)
        else:
            m = re.search(r"def\s+\w+", code)
            if m:
                code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name

    # —— JS 路径脚手架确定性修正（task_366 实锤，先于写盘：落库/布考场重建同源）——
    root_fixed = 0
    if is_js:
        for c in data["cases"]:
            c["code"], n_fixed = _fix_js_scaffold_paths(c["code"])
            root_fixed += n_fixed
        if data.get("imports"):
            data["imports"], n_fixed = _fix_js_scaffold_paths(data["imports"])
            root_fixed += n_fixed
        if root_fixed:
            log.info("gen_tests task=%s JS 路径脚手架修正 %d 处（__dirname '..' → 仓库根）",
                     task_id, root_fixed)

    # —— 方案C·条件缺口回灌（Gotel&Finkelstein post-traceability 的生成侧闭环）：
    # 确定性 2-gram 映射查无用例对准的条件 → 定向补生成恰 1 轮（预算纪律）——
    # 与 V4 行覆盖回灌不同层：这里是 RTM 条件层的"覆盖了≠测到了"信号。
    # 对话增补轮跳过（增补目标由反馈文本定向，不做全量条件扫描）。 ——
    cond_feedback: dict | None = None
    if config.COND_FEEDBACK_ENABLED and conditions and not feedback:
        new_cases, extra_imports, cond_feedback = refill_condition_gaps(
            llm, sources, args.get("requirement", ""), conditions, data["cases"], ws,
            used_names, config.GEN_METHODS_ENABLED, config.PBT_ENABLED,
            task_id=task_id, lang=lang)
        if new_cases:
            data["cases"] += new_cases
            if extra_imports:
                extra_imports, _ = _fix_js_scaffold_paths(extra_imports)
                old = (data.get("imports") or "").strip()
                data["imports"] = (old + "\n" + extra_imports).strip()

    # —— 方案A·断言质量审计（度量恒算）+ 可选闸：全弱断言用例 = 无证伪能力 ——
    # JS 分支：审计器是 Python AST，对 jest 代码会整案判"语法不可解析"→ gate 开时全剔。
    # v1 跳过（stats 标 skipped 诚实留痕）；JS 断言质量闸属后续工作。
    # java（白包阶段2）同理跳过：JUnit5 代码非 Python AST，闸=mvn test-compile。
    if is_js or is_java:
        gate_rejected, assertion_stats = [], {"cases": len(data["cases"]),
                                              "skipped": "js_v1" if is_js else "java_v1"}
    else:
        data["cases"], gate_rejected, assertion_stats = apply_assertion_gate(
            data["cases"], gate=config.ASSERTION_GATE_ENABLED)
    if gate_rejected:
        rejected += gate_rejected
    if not data["cases"]:
        return json.dumps({"generated": 0,
                           "reason": "全部用例未过断言质量闸（全弱断言，诚实降级）",
                           "rejected": rejected, "assertion": assertion_stats,
                           "cond_feedback": cond_feedback}, ensure_ascii=False)

    # —— §3.6 第 3 道闸：LLM 交叉语义审（V9，默认关；规则闸先行、LLM 闸收尾）——
    # 审查者角色逐案审断言是否对应需求，fail 单独重造 1 次、复审仍 fail 剔除，
    # 单案处置不拖累整批；审查不可解析 fail-open 放行（辅助闸不废整批生成）
    cross_info: dict | None = None
    if config.GEN_CROSS_REVIEW_ENABLED:
        data["cases"], cross_info = cross_review_gate(
            llm, data["cases"], args.get("requirement", ""), sources, ws,
            used_names, task_id=task_id, lang=lang)
        if not data["cases"]:
            return json.dumps({"generated": 0,
                               "reason": "全部用例未过交叉语义审（诚实降级）",
                               "rejected": rejected, "assertion": assertion_stats,
                               "cond_feedback": cond_feedback,
                               "cross_review": cross_info}, ensure_ascii=False)

    # —— 写入工作区（常规轮 test_gen_r{N}；对话增补轮独立 test_gen_fb{N}，不动已批准文件）
    # JS 用 .test.js / TS 用 .test.ts 后缀（jest 默认 testMatch），framework='jest' ——
    round_no = args.get("round_no", 1)
    if feedback and fb_no:
        fname, framework = gen_file_naming(lang, round_no, feedback=True, fb_no=fb_no)
        header_text = f"AI 增补用例（审查反馈第 {fb_no} 轮 · 两阶段评审对话）"
        version = int(args.get("version") or (100 + fb_no))
    else:
        fname, framework = gen_file_naming(lang, round_no)
        header_text = f"AI 生成用例（第 {round_no} 轮 · 五种方法知识库）"
        version = round_no
    header = f"// {header_text}" if (is_js or is_java or is_go or is_php) else f'"""{header_text}"""'
    imports = (data.get("imports") or "").strip()
    body = "\n\n".join(c["code"].strip() for c in data["cases"])
    content = (header + "\n"
               + (imports + "\n\n" if imports else "") + body + "\n")
    if is_java:
        # JUnit 方法片段拼接不合法（缺类包裹——py def / jest test() 片段可在顶层，
        # java 方法必须在 class 内）：渲染层补类壳，类名与文件名一致（surefire 扫描前提）。
        cls_name = Path(fname).stem
        indented = "\n".join(("    " + ln) if ln.strip() else ln for ln in body.splitlines())
        content = (header + "\n"
                   + (imports + "\n\n" if imports else "")
                   + f"public class {cls_name} {{\n{indented}\n}}\n")
        (ws / fname).parent.mkdir(parents=True, exist_ok=True)   # src/test/java/
    if is_go:
        # go func 片段可在顶层但不合法（缺 package 子句 + import 块）：渲染层补包壳。
        # package 名从 ws 根首个 .go 文件的 package 子句读取（同包测试前提）；
        # imports 字段规范化进 import() 块（lang_rules 约定每行一条 import 语句），
        # 空 → 最小 "testing"（*testing.T 签名必需）。
        # 跨轮撞名根修（Arm B 真机实锤）：R-Loop 轮 TestGenR2_test.go 与 TestGenR1_test.go
        # 同包共存，LLM 函数名撞 → redeclared 编译失败（java 靠类壳天然规避，go 包级
        # 函数无此隔离）——渲染层确定性加轮次前缀（TestR1_/TestR2_/TestRFb2_），跨轮唯一。
        marker = f"Fb{fb_no}" if (feedback and fb_no) else f"R{round_no}"
        body = re.sub(r"func(\s+)Test", rf"func\1Test{marker}_", body)
        pkg = _go_package_of(ws)
        import_lines = _normalize_go_imports(imports)
        content = (header + "\n"
                   + f"package {pkg}\n\nimport (\n"
                   + "".join(f"\t{ln}\n" for ln in import_lines) + ")\n\n" + body + "\n")
        # 漏 import 自检自补（658 实锤：漏 base64 整包 build failed=8 条用例假 0 执行）
        content, go_imports_added = _fix_go_missing_imports(content)
        if go_imports_added:
            log.info("gen_tests task=%s Go 漏 import 自动补: %s", task_id, go_imports_added)
        # 幻觉辅助函数手术（668 实锤：调用未定义 newFakeRepo→undefined 整包死+R-Loop 熔断）
        src_text = "\n".join(code for _, code in sources)
        content, go_stripped = _strip_go_undefined_calls(content, src_text)
        if go_stripped:
            log.warning("gen_tests task=%s Go 幻觉函数用例剔除: %s", task_id, go_stripped)
    if is_php:
        # PHP 方法片段必须包 TestCase 类（渲染壳，类名=文件名——phpunit 显式文件运行前提）。
        # imports 字段= require_once 语句（lang_rules 约定），原样置顶。
        cls_name = Path(fname).stem
        indented = "\n".join(("    " + ln) if ln.strip() else ln for ln in body.splitlines())
        # <?php 必须是文件第一条语句（declare(strict_types=1) 要求"very first statement"，
        # 头注只能放 <?php 之后——真机实锤：头注在前 3 轮 php -l 全废案）
        content = ("<?php\n" + header + "\ndeclare(strict_types=1);\n\n"
                   + (imports + "\n\n" if imports else "")
                   + "use PHPUnit\\Framework\\TestCase;\n\n"
                   + f"class {cls_name} extends TestCase {{\n{indented}\n}}\n")
    (ws / fname).write_text(content, encoding="utf-8")

    # —— 落库：脚本版本 + 用例清单（先于闸门，收集失败时可整体废案删除）；
    # filename 落列：两阶段"布考场"按审查结论重建生成文件的键 ——
    # 重跑幂等（delete-first，2026-11-15 闲时找bug 第一轮）：工作区同轮文件是
    # write_text 覆盖语义，DB 行必须同样覆盖——不清旧行则重跑翻倍（评审页/RTM
    # 用例数虚高）；module_id 接通 State 后同轮重插还会撞 UNIQUE(task_id, module_id,
    # version)。模式=同函数 collect 闸废案删除（先 cases 后 scripts）。
    conn.execute(
        """DELETE FROM test_cases WHERE script_id IN
           (SELECT id FROM test_scripts WHERE task_id=? AND filename=?)""",
        (task_id, fname))
    conn.execute("DELETE FROM test_scripts WHERE task_id=? AND filename=?",
                 (task_id, fname))
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, module_id, framework, version, filename, content, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, args.get("module_id"), framework, version, fname, content,
         hashlib.sha256(content.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    # —— C3（2026-11-27）：剔除项落库（enabled=0 + 原因）——"生成→剔除→落盘"漏斗
    # 此前只在消息里（且 `[generate 第N轮] {json}` 多数不可解析）不可审计；现在每个
    # 被闸剔除的用例在库里有行可查（source='ai_cull'，content=原因，enabled=0）。
    go_stripped = go_stripped if "go_stripped" in dir() else []   # 非 go 分支未定义
    cull_rows: list[tuple[str, str]] = []
    for x in (template_rejected or []):
        cull_rows.append((str(x)[:120], "模板展开失败"))
    for x in (rejected or []):
        cull_rows.append((str(x)[:120], "静态闸/断言质量闸剔除"))
    for x in ((cross_info or {}).get("dropped") or []):
        nm = x.get("name") if isinstance(x, dict) else str(x)
        rs = x.get("reason") if isinstance(x, dict) else ""
        cull_rows.append((str(nm)[:120], f"交叉语义审剔除：{rs}"))
    for x in (go_stripped or []):
        cull_rows.append((str(x)[:120], "Go 幻觉函数剔除"))
    for c in data["cases"]:
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content,
                                      priority, input, steps, expected, expected_basis)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (script_id, "ai", c.get("name", "unnamed"), c.get("method"), c["code"],
             c.get("priority"), c.get("input"), c.get("steps"),
             c.get("expected"), c.get("expected_basis")))
    for cname, creason in cull_rows:
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content, enabled)
               VALUES (?, 'ai_cull', ?, NULL, ?, 0)""",
            (script_id, cname, f"[已剔除] {creason}"))
    conn.commit()

    # —— 回灌容器（依赖随项目走，生成物也随项目走）。
    # 两阶段准备图里 generate 先于 provision（阶段一出题无考场）：容器不存在时不炸，
    # collect 闸与回灌如实标注"顺延"，由执行图 provision 在快照后补跑（collect_gate_files）。 ——
    collect_deferred: str | None = None
    try:
        container = runner.client.containers.get(args["container_name"])
        runner.copy_dir_into(container, ws)
    except Exception as e:  # noqa: BLE001 —— 容器缺失/守护进程未起：阶段一出题的合法形态
        collect_deferred = f"容器未就绪（{type(e).__name__}），collect 闸顺延到阶段二布考场"

    # —— 第二道闸（§3.6 第 2 道）：容器内可收集性检查 ——
    # Python: pytest --collect-only；JS: node --check（语法闸，jest 收集=语法可解析+require 可达，
    # require 失败在执行轮按 ImportError 归因进 repair，不在此预判）；
    # TS: tsc --noEmit 类型闸（白包阶段1，node --check 解析不了类型标注）。
    # 收集失败是文件级问题，无法按用例归责 → 整体废案（诚实降级），绝不把注定 ERROR 的生成物交给执行轮。
    r = None
    if collect_deferred is None:
        gate_cmd = collect_gate_cmd(lang, fname)
        r = runner.exec_in(container, gate_cmd, timeout_s=180)
    if r is not None and r.exit_code != 0:
        # 类型不匹配降级放行（2026-11-17 multi60 1660 nestjs 实锤）：tsc --noEmit 连编
        # 被测项目全部源码——真实项目常带类型债（ZodNullable/RoleType 不匹配），这类
        # 错误不是生成物的错（jest 运行时按文件隔离转译不受影响）。判据：错误行含
        # 生成物文件名但错误码全为类型不匹配类（TS2322 等）→ 放行继续执行；
        # 模块/名称找不到类（TS2304/TS2307）→ 生成物真错，仍废案。
        _err_text = (r.stdout or "") + "\n" + (r.stderr or "")
        _type_only = bool(re.search(r"error TS(2322|2345|2339|2352)", _err_text)) \
            and not re.search(r"error TS(2304|2307)", _err_text)
        if _type_only:
            log.warning("gen_tests task=%s tsc 类型不匹配（被测源码类型债，非生成物语法错）"
                        "——降级放行继续执行（诚实警告）: %s", task_id, _err_text[-160:])
            r = None  # 视作通过
    if r is not None and r.exit_code != 0:
        # 废案留档（诊断第一现场）：collect 闸失败多为生成物语法/类型错，闸只回退出码，
        # 诊断行号必须对着文件看（llm_parse_fail 同哲学：失败现场落盘供归因）
        try:
            _reject_dir = config.DATA_DIR / "reports" / "collect_gate_reject"
            _reject_dir.mkdir(parents=True, exist_ok=True)
            # fname 可能含子路径（java=src/test/java/X.java）——路径分隔符压平防静默失败
            _safe = fname.replace("/", "_").replace("\\", "_")
            (_reject_dir / f"task{task_id}_{_safe}").write_text(content, encoding="utf-8")
        except OSError:
            pass
        (ws / fname).unlink(missing_ok=True)
        conn.execute("DELETE FROM test_cases WHERE script_id=?", (script_id,))
        conn.execute("DELETE FROM test_scripts WHERE id=?", (script_id,))
        conn.commit()
        runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)
        log.warning("gen_tests task=%s collect 失败，本轮生成物废案（留档 %s）: stdout=%s stderr=%s",
                    task_id, fname, r.stdout[-300:], r.stderr[-200:])
        return json.dumps({"generated": 0,
                           "reason": "生成物未过容器内 --collect-only 可收集性检查（整体废案）",
                           "detail": (r.stdout + r.stderr)[-300:]}, ensure_ascii=False)

    summary = {"capability_mismatch": sut_mismatch,     # 放首位：400 字符截断下用户仍可见（419 教训）
               "generated": len(data["cases"]), "file": fname, "script_id": script_id,
               "static_rejected": rejected,
               # 用例明细（对话增补结果下拉的数据源）：每条是什么、验证什么、依据什么
               "cases": [{"name": c.get("name"), "method": c.get("method"),
                          "expected": (c.get("expected") or "")[:160],
                          "input": (c.get("input") or "")[:120],
                          "expected_basis": c.get("expected_basis")} for c in data["cases"]],
               "methods": [c.get("method") for c in data["cases"]],
               "template_rejected": template_rejected,
               "assertion": assertion_stats, "cond_feedback": cond_feedback,
               "root_fixed": root_fixed,
               "cross_review": cross_info, "source_skipped": source_skipped,
               "basis_downgraded": basis_downgraded, "phase": phase,
               "collect_deferred": collect_deferred,
               "scope_fallback": scope_fallback,
               "conditions_truncated": conditions_truncated}
    log.info("gen_tests task=%s → %s", task_id, summary)
    return json.dumps(summary, ensure_ascii=False)


def rebuild_gen_files_from_db(conn, task_id: int, ws: Path) -> int:
    """两阶段"布考场"：按审查结论（test_cases.enabled）重写工作区生成用例文件。

    以 test_scripts.filename 为键逐文件重建：从脚本原文 AST 中摘除已停用用例的
    FunctionDef（保留 imports/模块级代码——imports 丢失会让整文件废），返回重写文件数。
    常规轮/对话增补轮文件都处理；无脚本行的任务返回 0（老流程不受影响）。
    """
    rows = conn.execute(
        """SELECT id, filename, content FROM test_scripts
           WHERE task_id=? AND framework IN ('pytest', 'jest') AND filename LIKE 'test_gen%'
           ORDER BY id""", (task_id,)).fetchall()
    rewritten = 0
    for row in rows:
        fname = row["filename"]
        if not fname:
            continue
        if not fname.endswith(".py"):
            # JS 文件无 Python AST：v1 不做用例级摘除，原样写回（评审勾选结论留在库台账，
            # 执行层对 JS 暂不生效——Tier2 诚实边界，README 已注明）
            (Path(ws) / fname).write_text(row["content"], encoding="utf-8")
            rewritten += 1
            continue
        enabled = {r["name"] for r in conn.execute(
            "SELECT name FROM test_cases WHERE script_id=? AND enabled=1", (row["id"],))}
        try:
            tree = ast.parse(row["content"])
        except SyntaxError:
            continue
        kept = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                if node.name in enabled:
                    kept.append(node)
                # 已停用用例的函数体摘除；非用例函数（helper）一律保留
            else:
                kept.append(node)
        tree.body = kept
        (Path(ws) / fname).write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        rewritten += 1
    return rewritten


def collect_gate_files(runner, container, conn, task_id: int, ws: Path) -> list[str]:
    """阶段一顺延的收集闸（两阶段执行图 provision 快照后补跑）：逐生成文件做容器内收集闸
    （Python: pytest --collect-only / JS: node --check / PHP: php -l），失败文件整体废案，
    返回（废案文件名清单, 原因表{fname: 闸 stderr/stdout 尾部}）——C4（2026-11-27）：
    此前只记文件名（D2 夹具被废却查不到为何），原因只进 log 不进消息，无从取证。
    test_imported.* 只报告不删库（人工插入用例保留审计，交由人工在
    评审页处理）。
    2026-11-15 卡 3 取证实锤（1142）：旧清单只有 python/js 形态——go/java/php 生成文件
    不进清单 → 顺延闸对三语言空转 → run 时 [build failed] 无人拦截（TestR1 结果 0 条）。
    补：php 文件级闸（php -l 逐文件）；go/java 项目级闸（闸命令编译全项目/包——go build
    ./... && go test -c / mvn test-compile，一次跑全部生成文件，失败整组废案：闸只回退出
    码无法文件级归责，与 gen_tests 主路径"整体废案"同语义）。"""
    names = sorted(p.name for p in Path(ws).glob("test_gen_r*.py")) \
        + sorted(p.name for p in Path(ws).glob("test_gen_fb*.py")) \
        + sorted(p.name for p in Path(ws).glob("test_gen_*.test.js")) \
        + sorted(p.name for p in Path(ws).glob("TestGen*.php"))
    for imported_name in ("test_imported.py", "test_imported.test.js", "test_imported.php"):
        if (Path(ws) / imported_name).exists():
            names.append(imported_name)
    dropped: list[str] = []
    reasons: dict[str, str] = {}     # C4（2026-11-27）：废案原因随返回值出——此前只记
    for fname in names:              # 文件名（D2 夹具被废却查不到为何），原因只进 log
        gate_cmd = (["node", "--check", fname] if fname.endswith(".test.js")
                    else ["php", "-l", fname] if fname.endswith(".php")
                    else ["pytest", "--collect-only", "-q", fname])
        r = runner.exec_in(container, gate_cmd, timeout_s=120)
        if r.exit_code == 0:
            continue
        reasons[fname] = (r.stderr or r.stdout or "").strip()[-160:]
        dropped.append(fname)
        (Path(ws) / fname).unlink(missing_ok=True)
        runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)
        if fname not in ("test_imported.py", "test_imported.php"):
            conn.execute(
                """DELETE FROM test_cases WHERE script_id IN
                   (SELECT id FROM test_scripts WHERE task_id=? AND filename=?)""",
                (task_id, fname))
            conn.execute("DELETE FROM test_scripts WHERE task_id=? AND filename=?",
                         (task_id, fname))
            conn.commit()
        log.warning("collect_gate_files task=%s 废案 %s: %s", task_id, fname,
                    (r.stderr or "")[-160:])
    # 项目级闸（go/java）：闸命令本身编译全项目/包，文件级归责不了——整组一次。
    # 闸执行证据留痕（1142 教训：闸为何没拦住 [build failed] 无从定位——证据蒸发）。
    for lang, pats in (("go", ("TestGen*_test.go",)),
                       ("java", ("src/test/java/TestGen*.java",))):
        files = sorted(str(p.relative_to(ws)).replace("\\", "/")
                       for pat in pats for p in ws.glob(pat))
        if not files:
            continue
        r = runner.exec_in(container, collect_gate_cmd(lang, files[0]), timeout_s=300)
        if r.exit_code == 0:
            log.info("collect_gate_files task=%s %s 项目级闸通过（%s 个生成文件）",
                     task_id, lang, len(files))
            continue
        # C4：项目级闸整组废案同样带原因（go build/mvn 编译错误尾部）
        for fname in files:
            reasons[fname] = (r.stderr or r.stdout or "").strip()[-160:]
        dropped.extend(files)
        for fname in files:
            (ws / fname).unlink(missing_ok=True)
            conn.execute(
                """DELETE FROM test_cases WHERE script_id IN
                   (SELECT id FROM test_scripts WHERE task_id=? AND filename=?)""",
                (task_id, fname))
            conn.execute("DELETE FROM test_scripts WHERE task_id=? AND filename=?",
                         (task_id, fname))
        conn.commit()
        runner.exec_in(container,
                       ["sh", "-c", "rm -f " + " ".join(f"/work/{f}" for f in files)],
                       timeout_s=30)
        log.warning("collect_gate_files task=%s %s 项目级闸失败，整组废案 %s: %s",
                    task_id, lang, files, (r.stderr or "")[-200:])
    return dropped, reasons
