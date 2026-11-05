# TODO-06 容器网络隔离真机验证（2026-11-06 清账）

> 历史欠账（质检轮 P1-4b）：「容器网络隔离未动——需真机验证，列论文威胁边界 TODO」。

## 真机实测（mta-test:node20-jest，2026-11-06）

| 网络配置 | 外网（registry.npmjs.org） | 结论 |
|---|---|---|
| 默认 bridge（**平台现状**） | **可达（HTTP 200）** | 生成用例可外呼任意外网——数据外泄/外呼攻击面存在 |
| `--network none` | 不可达（EAI_AGAIN，DNS 均不可达） | 完全隔离，但 host.docker.internal 同时失效 |

## 结论与威胁边界表述（论文 6.9 用）

1. **平台 v1 现状=无网络隔离**：执行容器与宿主同享默认 bridge，用例运行期可外呼。
   已知缓解因素：依赖安装走显式白名单命令、镜像内工具链固定；残余风险=对抗/低质用例
   的外呼行为不受限。
2. **完全隔离与 DB 依赖用例互斥**：`--network none` 下 host.docker.internal 同步失效
   （407 的 cleandata 类用例必须连宿主 MySQL）——不能一刀切。
3. **v2 建议**（不实现，留 roadmap）：按项目类型分级——无 DB 依赖项目 `--network none`；
   有 DB 依赖项目走 host-gateway 白名单（只放行宿主网关，仍断外网）；
   provision·db 探针已具备依赖检测输入（6c06742），接线成本=runtime 参数。

复现：`docker run --rm mta-test:node20-jest node -e "fetch('https://registry.npmjs.org/-/ping')..."`。
