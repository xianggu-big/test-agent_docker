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

# —— L4 执行镜像（与 docker/ 下 Dockerfile 对应） ——
IMAGE_PY = "mta-test:py312"
IMAGE_JAVA = "mta-test:jdk17-maven"

# —— 容器默认资源限额（对应 container_jobs 表默认值） ——
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_MEM_LIMIT_MB = 1024
DEFAULT_JOB_TIMEOUT_S = 600          # 单次容器任务超时（秒）

# —— R-Loop 补测闭环预算（第 4 步启用） ——
MAX_REPAIR_ROUNDS = 3                # 同一模块最多补测轮数
FINGERPRINT_BREAKER = 2              # 相同失败指纹出现次数 >= 此值即熔断
COVERAGE_TARGET = 0.70               # 准出标准：行覆盖率阈值（分析阶段冻结进任务）

# —— LLM 配置（第 3~4 步启用；密钥走环境变量 DEEPSEEK_API_KEY，不进代码库） ——
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
