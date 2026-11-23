# test-agent 执行镜像 · Python 3.8 基座（2026-11-23 请示卡 A 路线：BugsInPy 2019 生态）
#
# 背景（docs/请示卡-BugsInPy扩量路线-20261122.md）：P0 预检 py312 直跑 2019 生态
# 通过率 1/21（nose 死于 3.10+/click 7 API/pytest 8 fixture 语法层层叠加=版本地狱）。
# py3.8 是 2019 生态原生环境：collections ABC 别名还在（3.10 才移除）、nose 1.3.7 可活
# （构建自检实锤）、pytest 6.2 接受老 fixture 语法。
#
# 镜像哲学（对齐 python-test 头注）：只装工具链，项目依赖随项目走——
# P0 预检/真格按 manifest 的 extra_deps 运行时装（任务容器默认 bridge 有网，TODO-06）。
# 首版把 pytest-httpbin/multidict/click7 烤进镜像被 brotlicffi 源码编译炸教做人
# （BugsInPy 用例级依赖长尾太宽）——回退到工具链最小集。
#
# 用途：BugsInPy 格的 P0 预检+真格执行（config.IMAGE_PY 经 MTA_PY_IMAGE 环境变量
# 覆盖——bench3 worker 按 item 注入；默认 py312 零变化）。其他语言/基准不动。
#
# 构建：docker build -t mta-test:py38 .
FROM python:3.8-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 工具链：pytest 6.2（2019 fixture 语法兼容末代）+ pytest-cov/coverage + per-test 超时
# （任务3 机制同源 signal 法）+ nose（老测试链依赖）+ 体检同款 requests/jsonschema/httpx
# （era 锁版本：httpx 新版要 3.9+）+ P0 实证 extra_deps 钉版（2026-11-23 py38 P0
# 11/21 格实证的组合——cookiecutter 防 click8 拉升/sanic C 扩展族/thefuck 依赖，
# 全有 cp38 wheel；pytest-httpbin 不烤[brotlicffi 源码编译炸，httpie 格本就 blocked]）
RUN pip install --no-cache-dir --retries 3 \
      "pytest==6.2.5" "pytest-cov==3.0.0" "coverage==6.5.0" pytest-timeout \
      nose \
      "requests<3" "jsonschema<4.18" "httpx<0.26" \
      "click==7.1.2" "jinja2<3.1" "MarkupSafe==2.0.1" \
      multidict httptools "uvloop==0.19.0" \
      psutil colorama six

# git（差分闸同款：A 轨容器内 checkout 对照 ref）
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git --version

WORKDIR /work
RUN useradd --create-home --shell /bin/bash tester \
 && chown -R tester:tester /work
USER tester

# 自检（fail-fast）：3.8 生态铁三角
RUN pytest --version \
 && python -c "import nose; print('nose', nose.__version__)" \
 && python -c "import pytest_timeout; print('pytest_timeout OK')" \
 && python -c "import requests, jsonschema, httpx" \
 && coverage --version \
 && git --version

CMD ["sleep", "infinity"]
