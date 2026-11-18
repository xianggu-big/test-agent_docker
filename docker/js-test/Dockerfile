# test-agent 执行镜像 v1 —— JavaScript/TypeScript 执行支持（jest：生成 → 执行 → 覆盖率）
#
# 设计约定（与 python-test 镜像一致）：
#   ① 只装工具链（jest + ts-jest + typescript），不装被测项目依赖 —— package.json 依赖
#      由「依赖检查 Skill」容器内 npm ci / npm install 装，依赖随项目走、不进镜像。
#   ② 全局 jest 供"项目没装 jest"的仓库也能跑生成用例（NODE_PATH 指向全局 node_modules）；
#      项目自带 jest 时 npx 优先用本地版本。
#   ③ 非 root tester；/work 快照目录；sleep infinity 常驻；docker exec 驱动。
#   ④ 输出契约：run 节点用 `jest --json --coverage`，宿主侧适配器把 JSON 转成
#      pytest 风格文本（passed/failed/FAILED 行/TOTAL 覆盖率），下游 evaluate/repair 零改动。
#
# 构建：docker build -t mta-test:node20-jest .
# 国内网络可选：docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com -t mta-test:node20-jest .
FROM node:20-bookworm-slim

ARG NPM_REGISTRY=https://registry.npmjs.org
ENV NPM_CONFIG_REGISTRY=${NPM_REGISTRY} \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NODE_PATH=/usr/local/lib/node_modules \
    CI=true

# git：差分闸（归因三闸 Task G）在容器内 git checkout 对照 ref 复跑失败子集——
# 真机验证实锤（2026-11-04）：node:20-bookworm-slim 不带 git，差分闸对 JS 任务
# 恒 no_git 不可用；bookworm-slim 的 apt 源自带 git，装上即修复
# procps（ps 命令）：StrykerJS 进程清理依赖——bookworm-slim 不带（2026-11-12 实锤：
# 临时容器单独装 procps 无效，必须进镜像；否则 Stryker 中段 spawn ps ENOENT 崩溃）
RUN apt-get update \
 && apt-get install -y --no-install-recommends git procps \
 && rm -rf /var/lib/apt/lists/* \
 && git --version && ps --version

# python3（F 卡 C5，2026-11-12）：L3 冒烟执行器=Python requests 打 127.0.0.1（同 C4 契约
# 架构：执行器语言无关，语言只决定服务启动）——node20-jest 原本无 python3，
# 与 java/php 镜像同源补法；pip 软链保证 `pip install requests` 原命令可用
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/pip3 /usr/local/bin/pip \
 && python3 --version && pip --version

# 变异轨（任务卡-F 阶段 1 #7，2026-11-12）：StrykerJS 钉版本（§6 坑预判 1 防漂移）
RUN npm install -g --no-audit --no-fund jest@29 ts-jest@29 typescript@5 @types/jest@29 \
 && npm install -g --no-audit --no-fund @stryker-mutator/core@8.6.0 @stryker-mutator/jest-runner@8.6.0 \
# ts-jest 嵌套依赖提升（2026-11-18 multi60 1660/1655 实锤：全局 ts-jest require('jest-util')
# 解析不到——jest-util/jest-config 嵌套在 jest 包的 node_modules 里，Node 从 ts-jest 目录
# 向上找顶层 node_modules 找不到。符号链接到顶层即根治（保留单一真源不复制）。
 && ln -s /usr/local/lib/node_modules/jest/node_modules/jest-util /usr/local/lib/node_modules/jest-util \
 && ln -s /usr/local/lib/node_modules/jest/node_modules/jest-config /usr/local/lib/node_modules/jest-config \
 && ln -s /usr/local/lib/node_modules/jest/node_modules/jest-diff /usr/local/lib/node_modules/jest-diff \
 && ln -s /usr/local/lib/node_modules/jest/node_modules/jest-message-util /usr/local/lib/node_modules/jest-message-util \
 && ln -s /usr/local/lib/node_modules/jest/node_modules/jest-mock /usr/local/lib/node_modules/jest-mock \
 && ln -s /usr/local/lib/node_modules/jest/node_modules/pretty-format /usr/local/lib/node_modules/pretty-format \
 && npm cache clean --force

WORKDIR /work
RUN useradd --create-home --shell /bin/bash tester \
 && chown -R tester:tester /work
USER tester

# 自检（fail-fast）：三件工具能跑
RUN node --version && jest --version && tsc --version

CMD ["sleep", "infinity"]
