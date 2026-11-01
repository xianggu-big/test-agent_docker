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

RUN npm install -g --no-audit --no-fund jest@29 ts-jest@29 typescript@5 @types/jest@29 \
 && npm cache clean --force

WORKDIR /work
RUN useradd --create-home --shell /bin/bash tester \
 && chown -R tester:tester /work
USER tester

# 自检（fail-fast）：三件工具能跑
RUN node --version && jest --version && tsc --version

CMD ["sleep", "infinity"]
