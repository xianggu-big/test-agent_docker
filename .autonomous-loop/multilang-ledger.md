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
- [14:30] **注入 bug FN 方向评测完成（任务 652，用户注入 5 bug 到 go-rest-api+任务书提示词重跑）**：652=62 过/11 失败（10 failed+1 error）。**GT 对照判定：TP=2（REST-01 分页向下取整——pagination TestNew/BuildLinkHeader 失败直接命中；REST-04 404→500——errors middleware_test+album TestAPI get_unknown/delete_verify 命中）+ Partial=1（REST-05 OFFSET/LIMIT 互换——API 层疑似命中但 repository 单测被 PG 缺失遮蔽，直接证据缺失）+ FN=2（REST-02 JWT 小时→分钟——无任何测试断言 exp 时长；REST-03 Length(0,12)——边界 13-128 区间无用例）+ FP=4（PG dial tcp env 失败被误判 product_defect——classify_failure 修复在 652 执行前已 commit，回放测试已锁）。**
- **最重要发现：AI 生成的 21 条用例 0 检出注入 bug**——全部通过。根因=expected_basis 无真值锚定时 AI 用例的预期值从**当前（buggy）源码推导**→对源码内 bug 结构性失明（迁就实锤家族）。注入 bug 的检出主力=项目自带测试（携带上游真值断言）。**平台级解法**：①需求文本显式预期（X2）②自带测试作为 oracle 源 ③A 轨 buggy/fixed 差分。检出率（执行面）2/5=40%，AI 生成面 0/5。
- 环境遮蔽：PG 缺失挡掉 repository 层全部测试（REST-05 直接证据缺失）——任务书"哪些测试因环境问题无法执行"的实锤答案。
- [15:40] **L2 口语形态实验格完成（任务 653，understand 真机首秀+结果）**：口语需求（"别太长/十几个字符合理/新增编辑要一致/没找到不是内部错误/token 别几小时就失效"）→ **plan 拆 7 条条件全部精准语义化**（"接受/拒绝阈值相同（同一长度）""查询不存在的专辑返回未找到语义"——口语→可验证条件的转写质量极高）→ D2 代审反馈**自主逼近检出方向**（要求"errors.Is(err, ErrNotFound)/映射后 404、显式断言非 5xx"+增补 7 条超长名称 13/129 对照用例）。**但阶段二结果：AI 用例仍 0 检出（62 过/11 败与 652 相同失败集）——失明的第二层根因浮出=用例层位错配**：D2 要求的 ErrNotFound/404 断言落在 **service mock 层**（该层未被注入，ErrNoRows 返回正确），注入 bug 在**错误映射中间件（HTTP 层）**——AI 用例集中在易写的单元层，**层位覆盖策略缺失**（单元/集成/HTTP e2e 无分布设计）。REST-04 的 HTTP 层检出仍由 repo 自带 TestAPI（get_unknown/delete_verify）完成。
- **L2 vs L0 对比结论（诚实）**：检出率无差异（AI 面 0/5 两轮相同）；**差异在流程上半段**——条件质量（7 条精准语义化 vs 75 条元指令混杂）与 D2 反馈质量（自主逼近检出方向）显著提升。失明根因双锁定=①expected_basis 无独立真值（源码现状推导）②用例层位错配。**解法升级：d2 反馈中的层位指令（"HTTP 端到端断言 404"）应驱动生成器产出对应层位用例**（层位感知生成=下一增强）。
- **[16:05] 653 追问辨析（用户问"是否说明 Go 缺测试工具"）→答案：不是工具缺失，是平台层位锚定缺失**（读 653 两条用例 693/694 原文实锤）：Go 生态抓 REST-04 类层间映射 bug 工具齐备零成本（标准库 httptest+gin test mode），同容器自带测试已实证 HTTP 层用例可写可跑可抓。**AI 用例实锤形态**：R1 轮（693）全落纯函数单元层（Validate 表驱动边界值）+手写 fakeRepo stub；Fb1 增补轮（694）**忠实执行了 D2 反馈字面**——`errors.Is(err, sql.ErrNoRows)` 断言真实出现且通过！但落在 service 层：service 透传 ErrNoRows 在 buggy 版本里本来就对，**stub 世界不含中间件=注入 bug 在测试执行链路里根本不运行**（自导自演家族温和形态：被测对象从 SUT 换成 LLM 自建 stub；关键字统计 mock=0 因为 Go 无 mock.patch 惯例走手写 stub 实现接口——Python N2 闸的检测形态在 Go 侧不适用，需新形态）。**关键对照=平台自己已有答案**：Python 种子自举 coupon-api=TestClient(app).post API 形态锚定后批量用例自动走 API 层——同样 LLM 有 HTTP 层正例锚点就走对层，Go 侧种子/正例机制未接线（SEED 格默认关且只覆盖 Python/JS）。**修复路径（平台侧四件，Go 生态零改动）**=①gen_tests 层位分布纪律注入（错误语义类条件必须 httptest 层断言）+httptest 正例锚定（P2 Go 版）②analyze/manifest 识别架构分层（router/middleware/handler/service）注入层位信息③Go stub 自证启发式（自定义类型实现 repo 接口+全部用例走 stub=低置信标记不剔除）④种子自举 Go 接线。论文素材=隐含假设清单 A 部新条目「默认层位假设」（层间映射缺陷在截断链路里不可见）+over-mocking/assertion-on-stub 文献挂钩+防御矩阵第一原则再实证（prompt 分工=提供信息：LLM 缺的是"错误映射在中间件层+HTTP 层怎么起"这个信息，不是能力）。
- **[16:25] 层位感知生成落地+对照格点火（用户令"去做吧"）**：**MTA_LAYER_AWARE 旗标（默认关）**→build_user_prompt(layer_aware=) 注入 LAYER_GUIDANCE_SECTION（层位覆盖纪律：①错误语义/状态码类条件必须至少 1 条 HTTP 层用例——起真实路由发真实请求断言状态码，禁止 service 层 stub 断言代替 ②全部用例不得落同一层 ③手写 stub 只许隔离外部环境不许替你产生被测行为 ④路由构造从被测源码 import 区定位+项目 go.mod 依赖可用——正对 653 实锤"AI 用例全落 service stub 层"）+语言骨架（go=httptest.NewRequest/NewRecorder/ServeHTTP 三件套标注库必可用/python=TestClient 按需适用/js=通用纪律无骨架防依赖幻觉；骨架尖括号占位+"从被测源码照抄"禁令防 A9 幻觉构造）。gen_tests 主轮接线（refill 补生成轮保持自由通道=单变量纪律）；单测 8（开关两态逐字节不变/go-python-js 分支/注入位置在条件段前/接线源码锁定）；回归 **795 过 1 跳**/守卫 138 OK/0 FINDING。**对照格 task 654 点火**（scripts/run_layer_aware_cell.py：L2 口语需求从 653 库内剥前缀逐字节复刻+UNDERSTAND=1+**D2 收敛放行替身**=review_decisions 计数稳定 20s 自动 enqueue exec——653 手动 POST /run 的等效动作固化进 driver，治 648/653 症状）。预期观察=①AI 用例层位迁移（httptest 形态出现否）②REST-04 AI 面检出（0/5→?）③无干预基线已实锤=652/653 两轮 34 条用例 0 条 HTTP 层（回答用户"不干预会不会自发"：不会，连 D2 指了方向都不升层）。成本预估 ~48k token/格（652=45935/653=47728）。
- **[17:35] LAYER_AWARE 对照格 658 结果=层位迁移成功但撞出三连真缺陷（全部已修）**：**658（唯一变量=层位注入）AI 用例形态彻底改变**——8 条用例全落 HTTP 层：httptest.NewRequest/NewRecorder/ServeHTTP+被测真实路由库 ozzo-routing 从源码 import 区照抄（零幻觉）+**auth.MockAuthHandler 都找到了**（自带测试同款 mock 认证）；手写 stub 类型数 0（对照 653 的 fakeRepo stub 世界）。**四条检出方向断言全写出**：GET unknown 断言 404+显式断言非 5xx（REST-04）/超长名称 400+create-update 一致性（REST-03）/分页 PageCount 对账（REST-01）/JWT claims 手解+validFor<2h 断言（REST-02）。**但 test_case_results ai=0 行——8 条用例 0 条执行**：漏 import `encoding/base64`（Login 用例手解 JWT claims）→整包 build failed→假 0 检出。**三连真缺陷+修复**：①**锁循环三连根因（conn-probe 诊断设施实锤，MTA_CONN_PROBE 默认关+悬挂>10s 自动 rollback 止血）**=enqueue INSERT 超时/post_prep_review_hook 的 D2 deps.conn 丢弃引用/generate_tests conn 异常路径——三处异常路径不 rollback→未提交事务悬挂持锁全进程锁死（654-657 四连废案元凶；**648/653 exec 未入队真根因=同一病根**）+产品修复三处（enqueue rollback/hook finally close/D2 except rollback）；②**go_adapter 证据蒸发**=编译错误走 stderr 不产生 -json 事件→build_err 恒空→message 裸 build failed（task_366 stderr 丢弃同族 Go 版）→修复=_fill_build_err_from_stderr；③**_fix_go_missing_imports**=落库前确定性补漏 import（17 标准库标识符点调用映射，命中才补）——**自抓自修实锤：首版 have 引号口径 bug（已 import 也重复补→imported and not used）被自己的单测抓住**。单测 +6（658 重放/已 import 不补/struct tag 不误触发/无块原样/stderr 进 message/无 stderr 兜底）；回归 **801 过 1 跳**/守卫 138 OK。**659 验证格在跑**（import 修复后 AI 用例应真执行——故事线：无干预 0/5→口语化 0/5→层位注入形态迁移→执行层修复→检出率?）。
- 待续（下轮接续入口）：**①D2 增补轮挂起排查（670 实锤：fb1 gen_tests 后 15min 无 LLM 活动不炸不退——卡点在 fb1 的容器 gate/repair 内环嫌疑，第一件事）②L3 半精确格+L4 精确格 → 三级检出率曲线（可与 MTA_LAYER_AWARE=1 叠加验证）③gin/pocketbase GOTOOLCHAIN 探针④PG sidecar 解封 repository 层⑤understand 留痕缺失排查（653 消息流无 [understand] 行）⑥generate_tests conn try/finally 卫生修（止血兜底中）⑦编译四层修复后的 AI 用例真实检出数字（故事线最后一环）**。全部代码改动已 commit e394a03；8000 server 实验期间关闭（用户自行决定恢复）。
