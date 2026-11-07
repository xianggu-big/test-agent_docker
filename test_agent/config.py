"""全局配置：路径、镜像 tag、预算参数。

原则：所有可调参数集中在这里；做实验时只改这里，不散落魔数。
实验预注册纪律：进入实验阶段后以下预算值冻结，中途不得修改。
"""
import os
from pathlib import Path

# —— 路径 ——
ROOT = Path(__file__).resolve().parent.parent          # 项目根目录
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "test_agent.db"                   # 第 5 层数据库
SCHEMA_PATH = ROOT / "docs" / "layer5-data-schema.sql"  # 建库脚本（22 张表）

# —— L4 执行镜像（与 docker/ 下 Dockerfile 对应；注册表供镜像管理/自动补齐用） ——
IMAGE_PY = "mta-test:py312"
IMAGE_JAVA = "mta-test:jdk17-maven"
IMAGE_JS = "mta-test:node20-jest"     # JS/TS 执行链路（jest Tier2，2026-11-01）
IMAGE_UI = "mta-test:playwright"      # UI 黑盒执行链路（黑盒能力补齐阶段3，2026-11-07）
IMAGE_BUILD_CTX = {
    IMAGE_PY: ROOT / "docker" / "python-test",
    IMAGE_JAVA: ROOT / "docker" / "java-test",
    IMAGE_JS: ROOT / "docker" / "js-test",
    IMAGE_UI: ROOT / "docker" / "playwright-test",
}
# 分级（analyze 判定）：可容器执行的语言 → 执行镜像；其余走 static_scan 兜底（Tier3）。
# typescript 升 Tier2（白包阶段1，2026-11-06）：镜像已全局备 jest+ts-jest+typescript，
# 散装 TS 走内联 transform 配置、带 package.json 项目由 install_deps --no-save 补装工具链，
# 收集闸=tsc --noEmit 类型闸；与 javascript 同走 node20-jest 镜像与 js_adapter。
# java=镜像已备、生成/执行链路未接（白包阶段2 接线）。
EXEC_LANGUAGES = {"python": IMAGE_PY, "javascript": IMAGE_JS, "typescript": IMAGE_JS,
                  "java": IMAGE_JAVA}

# —— 噪声目录（clone 快照 / 语言统计 / 源码收集共用一份定义） ——
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".idea"}

# —— 工作区快照预算（任务B/2026-11-01：#281 7.7GB 工作区 io.BytesIO 打 tar 宿主 OOM 实锤） ——
# copy_dir_into 尊重 .gitignore（pathspec）+ 内置排除 + 以下预算；总量超限复制前 fail-fast
SNAPSHOT_MAX_TOTAL_BYTES = 512 * 1024 * 1024   # 快照总预算（默认 512MB，超限报错不 OOM）
SNAPSHOT_MAX_FILE_BYTES = 64 * 1024 * 1024     # 单文件预算（超限跳过该文件并记录）
SNAPSHOT_EXCLUDE_DIRS = {                      # 内置排除（gitignore 之外的兜底；可追加）
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "target",
    ".gradle", ".cache", "htmlcov", ".next", ".nuxt", "coverage", "*.egg-info",
}

# —— 逐用例执行结果（任务C/2026-11-01：实时执行看板） ——
JUNIT_PATH = "/tmp/mta_junit.xml"              # 容器内 pytest --junitxml 输出（每轮覆盖）

# —— 容器默认资源限额（对应 container_jobs 表默认值） ——
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_MEM_LIMIT_MB = 1024
DEFAULT_JOB_TIMEOUT_S = 600          # 单次容器任务超时（秒）

# —— R-Loop 补测闭环预算（第 4 步启用） ——
MAX_REPAIR_ROUNDS = 3                # 同一模块最多补测轮数
FINGERPRINT_BREAKER = 3              # 相同失败指纹出现次数 >= 此值即熔断（2026-11-02 用户改：
                                     # 三次再熔断，多一轮自愈；X8 对照格同步调 4）
COVERAGE_TARGET = 0.70               # 准出标准：行覆盖率阈值（分析阶段冻结进任务）

# —— ②定向修复策略（调研报告 §8 行2/行3；三链路对比见 benchmarks/compare_pipelines.py） ——
# prune  = 链路A基线：剪坏用例+下轮全量重生成 ｜ rewrite = 链路B：带失败上下文重写（YATE）
# hybrid = 链路C：重写(预算+准入闸)+缺口回灌全量补测+剪除兜底 —— 优缺点合成路线
REPAIR_STRATEGY = "prune"
REWRITE_BUDGET = 3                   # 单轮重写条数上限（防重写风暴）

LEVELS_L3_ENABLED = False            # L3 系统测试半自动（D5：冒烟自动判定+失败人工介入）
LEVELS_L4_ENABLED = False            # L4 验收辅助（GWT 场景生成+人工确认+D6 转化回归）
                                     # 默认关=四级接力止于 L2，口径逐字节不变
# —— 实验开关（docs/链路方法测试文档.md 控制变量矩阵：每次实验只动一个，其余保持默认） ——
GEN_METHODS_ENABLED = True           # 五方法骨架注入 gen 提示词（变量 V2：False=自由发挥）
PLAN_ENABLED = True                  # plan/RTM 测试条件拆解（变量 V3：False=跳过 plan 节点）
COVERAGE_FEEDBACK_ENABLED = True     # 缺口回灌 generate 提示词（变量 V4：False=盲补）
GEN_BOUNDARY_ATTACK = False          # 边界攻击纪律（变量 V6：True=强制每函数至少 1 条边界外输入；
                                     # corpus-002 recall=0 的改进实验，默认关）
WITNESS_CONTINUE_ENABLED = False     # 证人冻结+继续补测（变量 V8：True=疑似产品缺陷不首轮停，
                                     # 断言失败证人冻结挂账、其指纹不参与熔断，继续补测至
                                     # 非证人全过+覆盖达标或预算耗尽；X 格 V2/V4 循环类开关的
                                     # 表达前提——证人首轮即停则循环机制无运行空间）
GEN_CROSS_REVIEW_ENABLED = False     # 生成用例 LLM 交叉语义审（变量 V9，vision §3.6 第 3 道闸）：
                                     # 审查者角色逐案审断言是否对应需求，fail 单独重造 1 次、
                                     # 复审仍 fail 剔除（单案处置不拖累整批）；审查输出不可解析
                                     # fail-open 放行；默认关=零 LLM 调用、口径逐字节不变

# —— 三方案改造开关（docs/设计方案对比实验.md：同基线同语料，各改一层，各带一个独有指标） ——
ASSERTION_GATE_ENABLED = False       # 方案A·断言质量闸：全弱断言用例在静态闸后剔除（锚点 2412.14137）
PBT_ENABLED = False                  # 方案B·属性测试第七法：PBT 骨架注入 + 容器/宿主装 hypothesis（锚点 Tanaka 2510.25297）
COND_FEEDBACK_ENABLED = False        # 方案C·条件缺口回灌：生成后 2-gram 查 RTM 缺口 → 定向补生成 1 轮（锚点 Gotel&Finkelstein）

# —— 方法组合扩展（docs/黑盒白盒方法组合与沙箱自适应设计.md；新法默认关，逐个开做消融） ——
TEMPLATES_ENABLED = True             # 槽位填充模板通道（T1/T2/T5）：基础方法结构先验，降 token 提过闸率
STATE_TRANSITION_ENABLED = False     # B4 状态迁移法（T4 迁移清单模板；锚点 Chow TSE 1978）
METAMORPHIC_ENABLED = False          # B8 蜕变法（MR 模式库提示词；锚点 Chen CSUR 2018）

# —— 沙箱自适应（同上设计文档 §6；用户三模式） ——
AUTO_PREPARE_ENV = False             # 模式③一条龙：True=provision 缺镜像时自动构建（开局等待）；
                                     # False=模式①手动感知（provision 诚实 stop 并提示）；模式②=界面「一键准备」始终可用

# —— LLM 配置（第 3~4 步启用；密钥走环境变量 DEEPSEEK_API_KEY，不进代码库） ——
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# —— 三源知识注入（docs/领域知识注入调研-20261031.md：C docstring/A 用法锚点/B 陷阱库，默认关做消融） ——
DOCSTRING_GROUNDING_ENABLED = False  # C源：目标函数 docstring 注入生成提示词（预期冲突时以 docstring 为准）
USAGE_EXAMPLES_ENABLED = False       # A源：仓库自带测试的调用形态注入（断言已剥离——只锚定 API 用法层，
                                     # 不锚定预期输出值；防迁就切分线，全过率上升=迁就发生应回滚）
PITFALL_KB_ENABLED = False           # B源：语言×类型陷阱清单注入错误推测法（curated 清单=文献空白点；
                                     # 发现二前车之鉴：很可能无效，负结果如实写，与发现二互证）

# —— 两阶段人机协作（docs/两阶段人机协作流程设计-20261031.md；任务级 two_stage 开关） ——
FEEDBACK_MAX_ROUNDS = 5              # 对话增补预算封顶：增补 ≤5 轮（防"再来一次"无限对话）
PREP_GEN_MAX_ROUNDS = 2              # 阶段一出题 0 条（LLM 偶发不可解析）自动重试上限——
                                     # 单阶段主图 repair_gen 兜底的两阶段对等物（E2E 实锤后补）

# —— AI 代审（docs/AI代审设计-20261031.md：任务级 ai_reviewer off/hybrid/auto） ——
AI_REVIEWER_TOKEN_BUDGET = 80_000    # 代审停车线（任务级）：D2 审查+增补循环 token 超线即停等人工（fail-closed）
AI_REVIEWER_MAX_ROUNDS = 5           # auto 模式 D2 审查→增补→再审循环上限（与 FEEDBACK_MAX_ROUNDS 同源语义）

# —— 种子自举模板（docs/种子自举模板设计-20261105.md：先造1条最简种子→容器collect验证→结构锚点注入批量；默认关） ——
SEED_BOOTSTRAP_ENABLED = False

# —— 防御矩阵 v2 新设计（docs/防御矩阵-prompt与非prompt措施-20261105.md §四：P1/P2/N1/N2 打包；
#    默认关=提示词与静态闸行为逐字节不变；F4 红队对抗格以此旗标测拦截率） ——
def _flag(env_key: str, default: bool) -> bool:
    """环境变量覆盖旗标：MTA_*=1/true/y 开，0/false/no/off 关，未设=代码默认。"""
    v = (os.environ.get(env_key) or "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


DEFENSE_V2_ENABLED = False
SUT_MANIFEST_ENABLED = _flag("MTA_SUT_MANIFEST", False)
# B2（任务卡-369：SUT 功能函数候选清单注入——治「纯数据文件被当被测单元」，
# 369 实锤 questions.js 被当 SUT 而真功能 app.js 零执行）
DEFECT_CLUSTERING_ENABLED = _flag("MTA_DEFECT_CLUSTER", False)
# 缺陷去重/同根因聚类（任务卡-369 E：确定性签名 sha1(failure_kind|error_class|
# 归一化消息|SUT首帧)[:12]；Sentry in-app frames/ReBucket 桶思想，零 LLM）

# —— 测试广度增强（docs 任务卡 .autonomous-loop/e9e10_daily/task_card_breadth_5q.md，2026-11-06） ——
# E9 实锤：plan 只看需求文本，条件全挤在核心函数域（task432 的 15 条件全落 password 模块）；
# 降采样纯按优先级保 15 同函数域挤占名额。两层治理（防御矩阵范式）：
# prompt 层=plan 提示词注入源码结构清单+广度纪律（信息提供）；闸层=降采样「15 优先级+≤5 函数域
# 分散补充」总 ≤20（E2E 实锤 20 条=DeepSeek 稳定输出上限）。默认关=提示词与闸行为逐字节不变。
BREADTH_SAMPLING_ENABLED = _flag("MTA_BREADTH", False)

# —— 四级双轨 P1（L2 集成层，docs/四级双轨测试框架研究规划.md §3.2/§3.5/§11） ——
LEVELS_ENABLED = False       # D9 串行任务接力总开关：True=API 下发在 L1 后自动接力 L2 leg

# —— E6 集成层消融三开关（S0，2026-10-31；默认 True=全轨，消融格 L2E1/L2E2/L2E3 逐个关） ——
L2_BLACKBOX_ENABLED = True   # 黑盒轨：契约确定性用例+需求断言用例（False=只留白盒/顺序场景）
L2_WHITEBOX_ENABLED = True   # 白盒轨：调用图+顺序场景（False=只留黑盒契约用例）
TRACE_ENABLED = True         # 跨级溯源确定性建链（False=build_task_traces 返回空+gaps 标注）
L2_SERVICE_PORT = 8000       # v0 单容器：被测服务与测试执行同容器，localhost 端口
L2_SERVICE_WAIT_S = 30       # 服务健康检查等待上限（秒）

# —— 失败归因三闸 v1（11.4 恢复计划 §3；研究先行引用见 skills/attribution.py 模块头） ——
# 生产接线（2026-11-05，开源调研：SWE-bench harness 同思路——开关经环境变量进入评估
# 环境而非改代码默认值）：代码默认保持关（526 基线保护），真实任务一键开启=
# MTA_ENV_GATE=1 / MTA_DIFF_GATE=1 / MTA_FLAKY_RERUNS=3
ENV_GATE_ENABLED = _flag("MTA_ENV_GATE", False)     # 环境闸：首轮真实用例前跑金丝雀（框架/IO 自检），失败 fail-fast 判环境无效
DIFF_GATE_ENABLED = _flag("MTA_DIFF_GATE", False)   # 差分闸：疑似产品缺陷在对照 ref 复跑失败子集（2×2 判定；要求工作区 .git）
DIFF_GATE_REF = "HEAD~1"    # 差分闸对照 ref（基准场景可指向已知 fix 提交）
FLAKY_RERUNS = int(os.environ.get("MTA_FLAKY_RERUNS", "0") or 0)
# 连跑：>0 时疑似产品缺陷终判前同命令连跑 N 次，结果不一致在 stop_reason 标 flaky

# —— 环境自适应 recipe（任务F / 2026-11-02，docs/环境自适应调研与设计.md §6） ——
RECIPE_ENABLED = False              # 默认关：关=EXEC_LANGUAGES 旧路径逐字节不变（可回滚）
RECIPE_MAX_BUILD_S = 900            # 单次镜像构建超时（秒）
RECIPE_MAX_DOCKERFILE_LINES = 40    # Dockerfile 行数预算（>此值拒）
RECIPE_MAX_IMAGE_MB = 2048          # 镜像大小预算（构建后 inspect 校验）
RECIPE_REPAIR_ROUNDS = 2            # 构建失败自修复轮数上限（失败日志回灌提案）
# 基础镜像池：recipe 只能从这里选（LLM 不得自造镜像名）
RECIPE_IMAGE_POOL = {
    "python": {"3.9": "python:3.9-slim", "3.10": "python:3.10-slim",
               "3.11": "python:3.11-slim", "3.12": "python:3.12-slim",
               "3.13": "python:3.13-slim"},
    "javascript": {"18": "node:18-bookworm-slim", "20": "node:20-bookworm-slim",
                   "22": "node:22-bookworm-slim"},
}
# 辅助镜像清单（镜像管理动态化 2026-11-07）：sidecar/recipe 基础池等「仅展示」镜像——
# 接入方式：可自动构建的注册进 IMAGE_BUILD_CTX（buildable=true）；仅作依赖/基础镜像
# 的进本清单（buildable=false，页面显示存在状态、无构建按钮）
AUX_IMAGES = ["mariadb:11", "postgres:16"] + sorted(
    {v for pool in RECIPE_IMAGE_POOL.values() for v in pool.values()})

# 允许的安装命令前缀（正则全匹配；其余一律拒绝——LLM 不能写任意命令）
RECIPE_ALLOWED_INSTALL = [
    r"pip install -r [A-Za-z0-9._/\-]+",
    r"pip install --no-cache-dir -r [A-Za-z0-9._/\-]+",
    r"pip install \.(\[dev\])?",
    r"pip install --no-cache-dir \.(\[dev\])?",
    r"pip install (pytest|coverage|pytest-cov|pytest-mock|pytest-asyncio|httpx|requests|hypothesis)(==[\w.]+)?",
    r"pip install poetry && poetry install( --no-root)?",
    r"npm ci",
    r"npm install --no-save --ignore-scripts( jest)?",
    r"npm install --ignore-scripts",
    # 测试工具链组合（多个白名单包一起装；仍限定在固定包集合内，不含任意第三方）
    r"pip install ((pytest|pytest-cov|coverage|pytest-mock|pytest-asyncio|hypothesis|"
    r"httpx|requests)( (pytest|pytest-cov|coverage|pytest-mock|pytest-asyncio|"
    r"hypothesis|httpx|requests))*)",
    r"yarn install --frozen-lockfile --ignore-scripts",
    r"pnpm install --frozen-lockfile --ignore-scripts",
]
