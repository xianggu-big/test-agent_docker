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
- [02:15] **锁持有者实锤（SysInternals handle.exe）**：pid 62380（8021 旧服务，昨晚 21:23 并行会话启动）持有 3 个 db 连接+多 wal 句柄，其一处于永久 RESERVED 写事务（旧代码无 write_conn 保护的历史 500 悬挂形态）；8000 新服务零 db 句柄；645/646 冻结排除"服务在跑任务"。10min+30min 探测均未自愈。
- [02:15] 解除条件（写死）：关闭/重启 8021 端口服务进程后，重跑 `python scripts/run_multilang_real.py run --priority ALL` 即断点续跑（8×5 需求已固化=控制变量资产齐）；冒烟 #2（tid 648）已到 ready_for_review，恢复后 POST /api/tasks/648/run 放行即可收尾。
- [02:15] findings.md 已产出中断版骨架（ESM 缺口/自带测试混入/D2 代审质量三项真实发现=冒烟先行 2 格换来）。
- [02:30] **【实验挂起判定】** 写锁持续 50+ 分钟（15s×3/60s/30min 探测全败，WAL 全程静止=事务永久 idle），自愈概率归零。按任务卡外部阻塞纪律挂起：本轮剩余 P1/P2/P3 跑批与数据聚合全部依赖真实库写，无合规绕行（tmp 库=明文红线禁；杀 8021=服务进程禁杀纪律；API 通道=服务进程自身写也被外部锁挡）。
- **本轮已固化资产**（解除阻塞后 100% 可续）：①manifest 8 项目×5 需求（控制变量核心资产）②driver 四命令完整（断点续跑）③冒烟 #1 全链路结论（ESM-only vs CJS 管线冲突=真实形态缺口，换血规避+findings 素材）④冒烟 #2 阶段一全通+D2 代审决策落库（tid 648 差放行一步）⑤findings 中断版骨架三项发现。
- **接续入口（精确）**：解除阻塞（重启/关闭 8021 端口服务）→ `python scripts/run_multilang_real.py run --priority ALL`（断点续跑）→ 冒烟补 1 格（class-validator req0 收尾，tid 648 POST /run 放行）→ P1(40)→P2(20)→P3(40) → `aggregate` 三件套。预计剩余跑批成本：P1≈80 万-200 万 token。
- [12:45] **平台增强落地（实验轮附产品）：understand 需求理解与提炼层**——用户拍板"任意形态输入（一句话/小白话/任务书）→ 提炼测试方向目标 → LLM 按目标定位要测的东西"愿景，实现 skills/understand.py（understand_requirement fail-open+render_understanding 注入段）+config.UNDERSTAND_ENABLED（MTA_UNDERSTAND 默认关=消融纪律）+plan 节点内联接线（产物 agent_messages 留痕+on_resp 落 llm_calls 记账）+build_plan_prompt understanding 参数（None=提示词逐字节不变）。**真实验收：完整 Go 任务书喂入——test_goals 零元指令泄漏（机器实证"缺陷报告/交付报告"等任务规范词零出现）+5 条显式假设+条件性范围诚实声明（"若存在 API"）**。单测 14（解析容错/fail-open/注入契约 None 逐字节不变/旗标默认关源码锁定/节点接线契约/审计回调）。回归 776 过 1 跳/守卫 138 OK。设计纪律：产物是策略不是条件（test_goals 只当 plan 上下文，条件仍锚真值清单——闸不放松）；论文新故事=输入形态鲁棒性（一句话/小白话/任务书 → 一致性对比），与 multilang 真实项目实验正交。
- [13:25] **锁解除（用户杀 8021）+ Go 整线真实验证 PASS（任务 650，uuid 单包）**：225 过/1 败/1 跳 1 轮收敛（49.8k tokens）——①**依赖下载通了**（GOPROXY 容器内可达，go-rest-api 5 依赖 BUILD_OK 实锤）②真实项目自带测试 226 条 99.6% 通过（与 TS commander ESM 全灭对照——Go 链路真实项目兼容性好）③1 条 AI 增补用例断言写反 → product_defect 证人如实上报+人工定性=设计工作流正确执行 ④skip 语义透传（uuid 自带 regression skip）。
- **649（用户 go-rest-api 任务）失败根因三层**（=真实项目第二冒烟，暴露平台缺口全记台账）：①**嵌套目录**：repo_url 带全角括号后缀+zip 解压嵌套，go.mod 不在工作区根 → go test 全废（用户侧修=路径指内层真根）②**多包放置缺口（平台）**：gen_file_naming go 固定写工作区根（单包语义），go-rest-api 是 internal/cmd 多包结构——生成用例需写入被测包目录+package 对齐（下一轮修）③依赖下载 ✓ 非坎（探针实锤）。
- [13:25] 用户跑批纪律落地：GEN_MAX_CASES=10（commit fc2825c）——每任务 ≤10 条用例，小步验证跑通整线勿铺量。
- [13:55] **GT 到位+评测性质反转（用户提供 D:\go_test_evaluation 三项目 GT）**：三个 GT 全部 defects=[]（干净原始快照：go-rest-api 0 缺陷/gin v1.12 0 缺陷/pocketbase v0.40.6 0 缺陷，均为逐文件人工审计结论）→ **评测性质=FP 误报纪律**（非 FN 检出率）——干净快照上平台任何"疑似产品缺陷"都是 FP。已有 5 个 FP 数据点全部归因到平台机制：651×4（PG 连接拒绝被误判 product_defect——classify_failure 缺 Go 环境信号）+650×1（AI 增补用例断言写反被当证人）。**回答用户核心问题：既不是 LLM 能力不行，也不缺"Go 常见 bug 用例模板"**——干净快照的正确输出就是零缺陷报告；缺的是①归因器懂 Go 环境信号（已修）②证人的 case_defect 自检（用例断言写反属用例侧，人工定性流程）③工程清单外的形态适配（gin 需 go>=1.26/pocketbase 需 go>=1.27——GOTOOLCHAIN=auto 探针下一轮；go-rest-api 多包放置）。
- [13:55] **FP 纪律修复落地（commit 见 log）**：classify_failure 加多语言环境信号（dial tcp...connection refused/ECONNREFUSED/no Go files in/getsockopt）→ env；651 真实失败文本回放 4 FP 机制转绿（tests/test_fp_discipline_go_env.py 6 条：651 回放+裸形态+Python ECONNREFUSED+真断言不受影响防误吞）。回归 787 过 1 跳/守卫 138 OK。
- 待办：①651 重跑（PG sidecar 起来后 4 条 env 失败应转绿——A 类闭环）②gin/pocketbase 的 GOTOOLCHAIN 探针（go1.22 auto 下载 1.26/1.27）③三个 GT 项目各跑 1-2 格 FP 评测（干净快照应零 product_defect）④"其它语言同法"=GT 驱动 FP 纪律评测扩展到 TS/Java/PHP（GT 齐后同法）。
