# 实验台账 · 多语言真实项目冒烟（multilang-real-smoke，2026-11-08）

> 性质：用户授权的实验窗口——run_task 全链路【允许执行】，仅限本轮创建的实验任务
> （repo 路径前缀 data/experiments/multilang/）。产物隔离：data/experiments/multilang/
> + data/reports/multilang_real/；与 E9/E10 台账/产物完全隔离（禁触碰）。
> manifest=data/experiments/multilang/manifest.json（需求固化处，控制变量）。

## 选型记录（2026-11-08 01:10）

- 拉取通道：codeload tarball HTTP 200 可达（git clone 历史核验不通）；8/8 项目拉取成功。
- 拒绝记录：zod 9.96MB 超 5MB 约束 ✗；jmustache 404（仓库路径不存在）✗；
  jackson-core 404 ✗；json-java=gradle 项目（平台 Java 链路=mvn）✗；
  Java-WebSocket=gradle+slf4j 依赖 ✗；email-validator 首名拼写修正后 ✓。
- 最终 8 项目（tarball 全部 <5MB、单语言单包、LICENSE 在位）：

| 语言 | 项目 | repo | license | tarball | 角色 | 核心可测逻辑 |
|---|---|---|---|---|---|---|
| ts | commander | tj/commander.js | MIT | 0.20MB | 代表 | CLI 选项解析/命令注册/帮助生成 |
| ts | chalk | chalk/chalk | MIT | 0.30MB | 次要 | ANSI 颜色组合/style 链/enabled 判定 |
| java | jsoup | jhy/jsoup | MIT | 0.79MB | 代表 | HTML 解析 DOM/属性/文本抽取 |
| java | joda-time | JodaOrg/joda-time | Apache-2.0 | 1.04MB | 次要 | 日期运算/格式化/周期 |
| go | go-humanize | dustin/go-humanize | MIT | 0.03MB | 代表 | 字节单位/相对时间/数值格式化 |
| go | uuid | google/uuid | BSD-3 | 0.02MB | 次要 | UUID 生成/解析/版本/命名空间 |
| php | semver | composer/semver | MIT | 0.02MB | 代表 | 语义化版本解析/比较/约束区间 |
| php | email-validator | egulias/email-validator | MIT | 0.01MB | 次要 | 邮箱校验语法树/警告分类 |

- 风险预登记：jsoup/joda-time 的 mvn test 依赖（junit 等）需容器内从阿里云源拉取（镜像已配）
  ——若拉取失败会如实报错（collect gate exit≠0），停车线接管挂起该语言。

## 执行进度

- [01:10] manifest 定稿（8 项目初版）。
- [01:15] 出题完成：8 项目 × 5 条需求固化进 manifest（控制变量达成；LLM 超额输出放宽为取前 5）。
- [01:16] driver 开发完成（scripts/run_multilang_real.py：gen-reqs/run/aggregate/status 四命令，
  断点续跑=结果 JSON 存在即跳过，停车线=连续 5 失败/失败率>30%/BudgetExhausted）。
  建任务列名两修（users.id/占位符数）。
- [01:21] **冒烟 #1（commander req0）失败定性 → TS 清单换血**：全链路机制跑通（190s/58.5k tokens/
  23 用例/3 轮 R-Loop/归因 case_defect）但 ①round1 全目录跑混入 commander 自带 209 条测试全败
  （**ESM-only 项目 vs 平台 C jest CJS 管线结构性冲突**——round>1 已圈定生成物自愈，但首轮预算被吃）
  ②生成用例 0 通过（import 形态系统性冲突）。探针核验：commander/chalk 均 "type":"module"——
  **TS 换血=class-validator（CJS 兼容/MIT/0.18MB/代表）+ts-results（CJS 默认/ISC≈BSD/0.07MB/次要）**，
  需求重出固化。ESM-only 冲突记为平台缺口（本轮不修，findings.md 作真实形态发现）。
- [01:26] 冒烟 #2（class-validator req0）跑批中（真实项目 npm install+全链路较久，后台运行）。
- 冒烟 #2 结论出来后放量 P1（40 格）。
- [01:52] **外部阻塞：test_agent.db 写锁被未知进程永久持有**——症状=读全通/写恒锁（3×15s+60s BEGIN IMMEDIATE 全败）/WAL 文件完全静止（无活跃写入者）/8000+8021 两服务写路径全 500（stop 端点也挂）/python 进程仅 2 个 uvicorn/无 DB 工具进程。锁起点=01:34-01:37（648 D2 钩子尾部 _reviewer_msg INSERT locked 起持续 35min+）。
- 已确认事实：tid 648 建成功且**阶段一全通**（class-validator clone/npm install/出题/ready_for_review）+ **D2 代审已实际完成**（review_decisions round2 落库：approve/insert 决策含真实 reasoning+条件证据引用——代审质量极高）——只差"自动入队阶段二"一步被锁打断；POST /run 放行 500。
- 处置：周期探测写锁自愈（10min 间隔，上限 30min）；期间产物固化（manifest 8×5 需求已固化=实验控制变量核心资产已落盘）。锁不解=实验挂起报告（外部阻塞，非本轮可修——禁杀进程红线）。
- 冒烟 #1 附带发现（已固化）：commander/chalk 均为 ESM-only——**真实 ESM 项目 vs 平台 C jest CJS 管线结构性冲突**（TS 清单已换血规避；缺口本身记 findings 素材）。

## 阶段性小结

（跑批过程中每语言完成后追加）
- [11-08 01:17] DRIVER-RETRY ts/commander/req0/n1: OperationalError: no such column: user_id
- [11-08 01:17] DRIVER-FAIL ts/commander/req0/n1: OperationalError: no such column: user_id（重试后仍失败，挂起该格）
- [11-08 01:18] DRIVER-RETRY ts/commander/req0/n1: OperationalError: 8 values for 7 columns
- [11-08 01:18] DRIVER-FAIL ts/commander/req0/n1: OperationalError: 8 values for 7 columns（重试后仍失败，挂起该格）
- [11-08 01:21] P1 ts/commander/req0/n1 tid=647 status=failed cases=23 pass=0/0/0 rounds=3 tokens=58505 kind=case_defect retry=0
- [11-08 01:37] DRIVER-RETRY ts/class-validator/req0/n1: OperationalError: database is locked
- [11-08 01:38] DRIVER-FAIL ts/class-validator/req0/n1: OperationalError: database is locked（重试后仍失败，挂起该格）
