"""全局配置：路径、镜像 tag、预算参数。

原则：所有可调参数集中在这里；做实验时只改这里，不散落魔数。
实验预注册纪律：进入实验阶段后以下预算值冻结，中途不得修改。
"""
from pathlib import Path

# —— 路径 ——
ROOT = Path(__file__).resolve().parent.parent          # 项目根目录
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "test_agent.db"                   # 第 5 层数据库
SCHEMA_PATH = ROOT / "docs" / "layer5-data-schema.sql"  # 建库脚本（13 张表）

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

# —— 实验开关（docs/链路方法测试文档.md 控制变量矩阵：每次实验只动一个，其余保持默认） ——
GEN_METHODS_ENABLED = True           # 五方法骨架注入 gen 提示词（变量 V2：False=自由发挥）
PLAN_ENABLED = True                  # plan/RTM 测试条件拆解（变量 V3：False=跳过 plan 节点）
COVERAGE_FEEDBACK_ENABLED = True     # 缺口回灌 generate 提示词（变量 V4：False=盲补）

# —— LLM 配置（第 3~4 步启用；密钥走环境变量 DEEPSEEK_API_KEY，不进代码库） ——
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
