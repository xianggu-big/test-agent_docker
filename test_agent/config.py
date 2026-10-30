"""全局配置：路径、镜像 tag、预算参数。

原则：所有可调参数集中在这里；做实验时只改这里，不散落魔数。
实验预注册纪律：进入实验阶段后以下预算值冻结，中途不得修改。
"""
from pathlib import Path

# —— 路径 ——
ROOT = Path(__file__).resolve().parent.parent          # 项目根目录
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "test_agent.db"                   # 第 5 层数据库
SCHEMA_PATH = ROOT / "docs" / "layer5-data-schema.sql"  # 建库脚本（22 张表）

# —— L4 执行镜像（与 docker/ 下 Dockerfile 对应；注册表供镜像管理/自动补齐用） ——
IMAGE_PY = "mta-test:py312"
IMAGE_JAVA = "mta-test:jdk17-maven"
IMAGE_BUILD_CTX = {
    IMAGE_PY: ROOT / "docker" / "python-test",
    IMAGE_JAVA: ROOT / "docker" / "java-test",
}

# —— 噪声目录（clone 快照 / 语言统计 / 源码收集共用一份定义） ——
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".idea"}

# —— 容器默认资源限额（对应 container_jobs 表默认值） ——
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_MEM_LIMIT_MB = 1024
DEFAULT_JOB_TIMEOUT_S = 600          # 单次容器任务超时（秒）

# —— R-Loop 补测闭环预算（第 4 步启用） ——
MAX_REPAIR_ROUNDS = 3                # 同一模块最多补测轮数
FINGERPRINT_BREAKER = 2              # 相同失败指纹出现次数 >= 此值即熔断
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

# —— 四级双轨 P1（L2 集成层，docs/四级双轨测试框架研究规划.md §3.2/§3.5/§11） ——
LEVELS_ENABLED = False       # D9 串行任务接力总开关：True=API 下发在 L1 后自动接力 L2 leg

# —— E6 集成层消融三开关（S0，2026-10-31；默认 True=全轨，消融格 L2E1/L2E2/L2E3 逐个关） ——
L2_BLACKBOX_ENABLED = True   # 黑盒轨：契约确定性用例+需求断言用例（False=只留白盒/顺序场景）
L2_WHITEBOX_ENABLED = True   # 白盒轨：调用图+顺序场景（False=只留黑盒契约用例）
TRACE_ENABLED = True         # 跨级溯源确定性建链（False=build_task_traces 返回空+gaps 标注）
L2_SERVICE_PORT = 8000       # v0 单容器：被测服务与测试执行同容器，localhost 端口
L2_SERVICE_WAIT_S = 30       # 服务健康检查等待上限（秒）
