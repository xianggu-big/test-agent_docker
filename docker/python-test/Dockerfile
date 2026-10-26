# test-agent 执行镜像 v1 —— Python 深度支持（pytest 全链路：生成 → 执行 → 覆盖率）
#
# 设计约定（对齐借用框架 mta-sandbox:node24 的模式）：
#   ① 只装工具链，不装任何被测项目依赖 —— requirements.txt 由「依赖检查 Skill」在容器内
#      运行时装（pip install -r /work/requirements.txt），依赖随项目走、不进镜像。
#   ② 非 root 用户 tester；/work 为快照落地目录（宿主 clone 后 docker cp 进来，沙箱内不跑 git）。
#   ③ 常驻容器（sleep infinity），由调度器 docker exec 驱动，跑完 --rm 收尾。
#   ④ 资源限额不在镜像里 —— 由启动参数控制（docker run --cpus --memory），
#      归 L4 调度器管，镜像保持薄。
#
# 构建：docker build -t mta-test:py312 .
# 国内网络可选：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t mta-test:py312 .
FROM python:3.12-slim

# ARG 同时作用于构建期与运行期（ENV 透传）：运行时装项目依赖走同一个源
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 执行：pytest + 覆盖率；静态/安全扫描：ruff + bandit（低占比语言与 Python 静态检查共用）
RUN pip install --no-cache-dir --retries 3 \
      pytest pytest-cov coverage \
      ruff bandit

WORKDIR /work
RUN useradd --create-home --shell /bin/bash tester \
 && chown -R tester:tester /work
USER tester

# 自检（fail-fast）：镜像入库前确认四件工具都能跑，装坏了构建期就报错
RUN pytest --version \
 && coverage --version \
 && ruff --version \
 && bandit --version

CMD ["sleep", "infinity"]
