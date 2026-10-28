# 开源软件测试流程架构对比调研（工程实现侧，面向毕设"相关工作"）

> 日期：2026-10-28｜方法：三路并行实读 GitHub README / 官方文档 / 核心源码 / 论文页（非凭记忆），星数与活跃度均为"截至 2026-10 大致值"。
> 定位：与 `docs/industry-testing-research.md`（论文/标准侧，33 信源）**互补**——本篇覆盖**开源工程实现侧**，聚焦架构与流程设计，供论文相关工作/系统设计对比章使用。
> 纪律：关键断言附来源 URL；不可核验处如实标注；发现 7 条流传讹误，见 §8 引用纠错清单（**写作前必读**）。

---

## 0. 一页结论

1. 八个项目可归入**四个架构范式**：搜索驱动（EvoSuite/Pynguin/CodaMosa）、LLM 生成+覆盖验收（CoverUp/qodo-cover）、通用 agent 自由探索（SWE-agent，判定外包给基准）、固定图编排+台账硬判定（Hercules；Keploy 是"无 LLM 确定性管线"的对照极）。
2. **本项目属于第四范式**（LangGraph 固定图、LLM 产物化），且是唯一同时具备：需求→测试条件→RTM 追溯层、预言纪律（expected_basis+证人冻结）、失败归因三分类+变异标定、评估治理内建（确定性指标+双闸回炉+盲测协议）的项目。
3. 全部八个对照项目**没有任何一个**同时具备这四件事；其中"防恒真断言"这一条，LLM 系两家（CoverUp/qodo-cover）实测提示词零防护——arXiv:2412.14137 的批判直接命中，本项目三条纪律是其工程回应。
4. 本项目诚实短板：单语言（Python）单机、微型被测对象、无 CI 集成、不治理 flaky、语料规模小（9 缺陷）、单 LLM 供应商依赖（DeepSeek 402 实证）。
5. 一个独立佐证：TestZeus Hercules 1.0.0（2026-07-16）把运行时从 AG2 **迁移到 LangGraph** 并采用 planner/executor/assertion 固定状态机——与本项目"固定图骨架、LLM 产物化"的选型殊途同归。

---

## 1. 调研范围与方法

- 项目集选取原则：①SBST 经典基线（论文相关工作按惯例从它们开场）；②LLM 反馈闭环（与本项目 R-Loop 直接谱系）；③Agent 化测试平台（架构哲学对照）；④录制式范式（反衬"生成式"边界）。
- 方法：WebFetch 实读 GitHub 仓库页、README、docs（含序列图、提示词 toml、DB schema 源码）、官方文档站、论文 arXiv/DOI 页。星数/最近提交为 2026-10-28 前后实查。
- 本项目基准事实来自项目 `AGENTS.md`（五层架构、9 节点图、15 表、R-Loop、证人冻结、指纹熔断、E1-E5 实验）。

---

## 2. 四种架构范式总览

| 范式 | 代表 | 生成算子 | 反馈信号载体 | 成功/失败判定 |
|---|---|---|---|---|
| P1 搜索驱动（SBST） | EvoSuite、Pynguin、CodaMosa（骨架） | 遗传变异/DSE | 覆盖适应度直接驱动进化（无语义层） | 回归式 oracle（当前行为即真值） |
| P2 LLM 生成+覆盖验收 | CoverUp、qodo-cover | LLM | 覆盖缺口**翻译成自然语言 prompt** 回灌 | 测试通过+覆盖提升（验收门） |
| P3 通用 agent 自由探索 | SWE-agent/mini-swe-agent（+SWT-bench 判定） | LLM 自由决策 | 环境输出全文进上下文 | **外包**给外部基准（F2P/P2P） |
| P4 固定图编排+台账 | Hercules（本项目同谱系） | LLM 为节点产物 | 图结构定义信息流 | 内建硬判定（断言台账可推翻自称成功） |
| P0 对照：确定性管线（无 LLM 主循环） | Keploy | eBPF 录制回放 | 无（录制即真值） | 回放响应与录制逐字段比对 |

P1→P2 的本质分水岭：反馈回路里多了一个**非确定的语义翻译层**（覆盖数据→prompt），这是 LLM 系不可复现性的根源。P3 与 P4 的分水岭：决策权在 LM 自由探索还是在图结构内受约束。本项目取 P4，并额外补齐 P1/P2 都没有的**需求维度**与**评估治理**。

---

## 3. 逐项目架构速写

### 3.1 EvoSuite（Java SBST 事实标准，准维护）
- [EvoSuite/evosuite](https://github.com/EvoSuite/evosuite)，LGPL-3.0，~920★；最后 release 2021-09（v1.2.0），主干提交至 2025-02。论文：ESEC/FSE 2011（DOI 10.1145/2025113.2025179）。
- 架构：染色体（套件/单用例两模式）+ 23 个覆盖准则枚举（默认 8 准则组合：LINE/BRANCH/EXCEPTION/WEAKMUTATION/OUTPUT/METHOD/METHODNOEXCEPTION/CBRANCH，源码 `Properties.java` 实读）+ DynaMOSA 动态多目标搜索（1.1.0 起默认）+ 独立 runtime jar + 回归断言生成 + 套件最小化 + statistics CSV。
- 循环：搜索循环即反馈循环（默认预算 60s）；**无修复语义**——搜索结束不做失败归因、不迭代改写。
- oracle：回归式（README 原话 "regression assertions that capture the current behavior"）→ 测试按构造通过，**结构上不能宣告产品缺陷**。
- 隔离：JVM SecurityManager 沙箱 + 防 flaky 脚手架（mock 非确定性/重置静态状态）+ 单测 4s 超时；官方 Docker 镜像。
- 评估：覆盖率 CSV + 适应度曲线 HTML；无需求维度、无归因台账。

### 3.2 Pynguin（Python SBST 标准研究平台，高度活跃）
- [se2p/pynguin](https://github.com/se2p/pynguin)，MIT（0.30.0 起，此前 LGPL），~1.4k★；0.48.0（2026-10-15），月度发版。工具论文：**ICSE Companion 2022 pp.168–172**（arXiv:2202.05218）——**常被误引为 ISSTA 2022**。
- 架构（官方 dev/overview 七段）：模块分析（类型推断 TypeSystem/InferredSignature）→ Test Cluster → 自后向用例构造 + GA（算法可插拔：DynaMOSA/MOSA/MIO/WHOLE_SUITE/RANDOM + **LLM/LLMOSA/LLDYNAMOSA** 新算法）→ 字节码即时插桩执行（分支距离/Levenshtein 距离）→ 断言生成（**默认变异分析筛选：只保留能杀变异体的断言**）→ 最小化。
- 工程亮点：master-worker 崩溃自愈（worker 崩溃自动降级重启）；非期望异常标 `pytest.mark.xfail(strict=True)` "clear hint to manually inspect"——八个项目里最接近"审慎上报"的设计。
- oracle：回归式 + 变异分析提质；隔离：无强沙箱（README 红字警告可清盘 + `PYNGUIN_DANGER_AWARE` 门禁 + 建议 Docker + 子进程/内存/超时限制）。
- 自我定位："only a research prototype"。

### 3.3 CoverUp（覆盖引导 LLM 闭环的学术标杆）
- [plasma-umass/coverup](https://github.com/plasma-umass/coverup)，Apache-2.0，116★，pushed 2026-04。论文：FSE 2025（arXiv:2403.16218，DOI 10.1145/3729398）——**注意仓库不是流传的 sawrd/coverup（404）**。
- 架构：SlipCover 覆盖测量 → 未覆盖段 AST 切分（≤50 行）→ 对话式 LLM（asyncio 并行+限速，gpt-4o、temperature=0）→ pytest 执行验证 → 保留"通过且提升覆盖"用例。
- 反馈闭环：**三路信息回灌同一对话**（未覆盖行/分支标签 + 错误信息求改写 + `get_info` 工具让 LLM 索取定义源码）；每段对话硬上限 1+2 次 prompt；多轮重跑再聚焦。消融实验：错误修复反馈贡献最大（去掉损失 14–37%）。
- oracle：LLM 自写断言，prompt 仅"要求含断言"，**无任何防恒真/防跟随机制**；实测覆盖领先（中位 line+branch 80% vs CodaMosa 47%；整体 89% vs MuTAP 77%），比 CodaMosa 快 ~18 倍（成本 $399 vs $265，账目诚实）。
- 隔离：fork() 逐测试隔离（pytest-cleanslate）+ flaky 检测（pytest-repeat）；默认宿主直跑。持久化：测试文件 + G/F/U/R 计数，无台账。

### 3.4 CodaMosa（LLM 作搜索"点火器"的混合范式，已开源）
- [microsoft/codamosa](https://github.com/microsoft/codamosa)，MIT 叠加于 Pynguin 0.19 fork（LGPL），165★，2026-10-27 仍在更新。论文：ICSE 2023 pp.919–931（DOI 10.1109/ICSE48619.2023.00085）——**无 arXiv 版（2305.04115 是别的论文），代码已开源**。
- 架构 = SBST 骨架 + LLM 种子注入：主循环跟踪无进展迭代数，超过 `max_plateau_len` 判定覆盖平台期 → 按函数覆盖统计挑选未覆盖函数定向问 LLM → 种子包装成染色体进 NSGA-II 选择。**自适应熔断**：一轮 LLM 无种子存活则平台期长度翻倍（省配额的干净机制）。
- 输出修复：outputfixers.py 把任意 LLM 语句扁平化为可解析赋值序列（token 截断则逐行回退删到 `ast.parse` 通过）；示例配置甚至 `--assertion-generation NONE`（种子只求执行覆盖，不求断言）。
- oracle：回归断言由 Pynguin 事后捕获——**按构造跟随实现**。隔离：Docker 强制（/input 只读挂载、/output 产物）。持久化：覆盖时间线 CSV + replication 包。

### 3.5 qodo-cover（原 CodiumAI cover-agent，工业级但已停维护）
- [qodo-ai/qodo-cover](https://github.com/qodo-ai/qodo-cover)，AGPL-3.0，5,677★，**2025-06-15 README 官宣 "no longer maintained"**。
- 架构（官方序列图实读）：四组件 = Test Runner / Coverage Parser / Prompt Builder / AI Caller；流程 = 基线覆盖 → LLM 分析套件结构与插入点 → 主循环（LLM 产 YAML 测试 → 逐个 validate：通过且覆盖提升则保留，否则**回滚测试文件+记录失败** → 重新解析覆盖决定是否继续）→ 目标覆盖率或 max-iterations 终止。
- 失败处理：回滚式丢弃（失败列表回灌 prompt 但**不含错误堆栈**）；oracle：LLM 自写断言，**实读 test_generation_prompt.toml 证实零防护**（无系统提示词、无防平凡断言指令）——2412.14137 批判的直接命中对象。
- 亮点（对照项目中最强）：**每次尝试一条完整 SQLite 台账**（unit_test_generation_attempts 表：prompt/test_code/stdout/stderr/exit_code 全存）+ LLM 响应录制回放省钱 + 多语言 + CI 友好——形态上最接近"审计台账"，但无规则闸复审、无指标确定性承诺。

### 3.6 SWE-agent（通用修复 agent harness 事实标准）
- [SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent)，MIT，20.7k★；NeurIPS 2024（arXiv:2405.15793）。**已转 maintenance-only，继任者 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)（8.9k★，100 行核心，自报 SWE-bench verified >74%）**。
- 架构核心是 **ACI（Agent-Computer Interface）设计**：带 lint 的编辑命令（语法错误直接写不进）/ 100 行分页文件查看器 / 搜索只列文件名不展示上下文（实验发现更多上下文是干扰）/ 空输出显式化文案。scaffold = SWEEnv（SWE-ReX 薄封装，本地/远端 Docker/Modal 部署抽象）+ YAML 配置 agent + 历史压缩。
- 判定：**外包**——补丁交给 SWE-bench 式评测器（golden FAIL_TO_PASS/PASS_TO_PASS）；submit 时自审（重放复现脚本、git checkout 还原测试改动）是提示级软机制。终止 = submit 动作 + 每实例成本上限（如 $2/实例）。
- 隔离：每实例一个容器/远端沙箱，百级并行；轨迹全量落盘可回放。**无测试设计方法论、无 oracle 处理**——测试在其体系里是判定仪器而非产物。

### 3.7 SWT-bench（"测试生成能力"独立成基准）
- [logic-star-ai/SWT-Bench](https://github.com/logic-star-ai/SWT-Bench)（原 eth-sri/swt-bench 已迁移），MIT，92★；NeurIPS 2024（arXiv:2406.12952）。
- 任务定义：给代码库+issue，**写复现测试**——在 buggy 代码上失败（F→P）、在 gold patch 后通过、不得引入新失败；1,983 实例 + Lite 276。
- 关键结论：①修复 agent 改一句指令变测试生成，反超专用测试生成系统（SWE-agent+ 18.5% vs ZeroShot 3.6%）；②**生成的测试可作修复候选过滤器：SWE-agent 精度翻倍至 47.8%（recall 20% 代价）**——为"生成用例需二次审查/预期依据"提供量化论据。
- 意义：证明"测试生成质量"本身可独立基准化——本项目 X 格盲测语料协议是同思想的领域化小尺度实现（出题人/答题人/阅卷人三方隔离）。

### 3.8 TestZeus Hercules（开源 E2E 测试 Agent 平台，与本项目同范式）
- [test-zeus-ai/testzeus-hercules](https://github.com/test-zeus-ai/testzeus-hercules)，AGPL-3.0 open-core，1.2k★；1.0.0（2026-07-16）**运行时从 AG2 迁移到 LangGraph**。
- 架构（docs/architecture.md）：六层 = 核心 agent 回路（**planner/executor/assertion 三类节点状态机，planner 输出严格 JSON：plan/next_step/target_helper/terminate/assertion**）/ API 验证层 / 移动层 / MCP server（审批门控 plan 执行）/ 测试智能层（代码摘要→PRD→测试计划）/ 编排仪表盘。输入 = Gherkin 自然语言用例；覆盖 UI/API/SQL/安全（nuclei）/无障碍（WCAG）/视觉/移动仿真七类。
- **硬否决语义（与本项目同构）**：断言台账（ledger）生成 JUnit；文档原话 "A run that claims success while the ledger holds a failed assertion is overruled and reported as a failure"——agent 自称成功但台账有失败断言时强制改判失败。证据链完整（截图/视频/网络日志 proofs）。
- 弱点：隔离弱（进程组级，被测应用不经容器编排）；性能声明（"60x faster"等）**无第三方佐证**，README 的 "world's first" 是首创声明而非排行榜第一。

### 3.9 Keploy（录制回放范式对照，无 LLM 主循环）
- [keploy/keploy](https://github.com/keploy/keploy)，Apache-2.0，Go，18.9k★，三项目中最活跃（主仓 2026-10-28 仍有更新）。
- 架构：**socket 层录制-回放管线**——Linux 用内核 eBPF 探针（需 root）捕获全部网络交互 → 存为测试用例 + 依赖响应存为 mock → 离线确定性回放（时间冻结 Time Freezing + 噪声字段检测防假失败）。语言无关、零代码侵入；协议覆盖 gRPC/Kafka/DB 等极广。
- oracle：回归式（回放响应 vs 录制响应逐字段比对）——**固化录制时点行为，不能发现录制时就错的实现，不适用新需求验证**。AI 能力（边界值扩展、云端生成器）在 SaaS 侧闭源；本地 "Keploy Agent" 是代理二进制非 LLM 决策体。

---

## 4. 八维横评（含本项目）

| 维度 | EvoSuite | Pynguin | CoverUp | CodaMosa | qodo-cover | SWE-agent | SWT-bench | Hercules | Keploy | **本项目** |
|---|---|---|---|---|---|---|---|---|---|---|
| 范式 | P1 搜索 | P1 搜索 | P2 LLM+验收 | P1+LLM点火 | P2 LLM+验收 | P3 自由探索 | 基准 | **P4 固定图** | P0 录制回放 | **P4 固定图** |
| 需求层 | 无 | 无 | 无 | 无 | 无 | 无 | issue 文本 | Gherkin（无矩阵） | spec（无矩阵） | **plan+test_conditions+RTM 快照（独有）** |
| oracle | 回归 | 回归+变异筛断言 | LLM 自写·零防护 | 回归·跟随实现 | LLM 自写·零防护 | 外包基准 | F2P 金标 | 断言台账硬否决 | 录制比对 | **expected_basis+证人冻结+双闸审查** |
| 报缺陷能力 | 无（结构上） | xfail 转人工 | 无机制 | 无机制 | 无机制 | 间接 | F2P 即证据 | 有 | 无 | **product_defect 带证据链如实停报** |
| 失败用例处理 | 搜索淘汰 | 搜索淘汰 | 对话内修复 | 种子自然淘汰 | 回滚丢弃 | （修产品代码） | — | auto-healing | self-healing | **定向重写·保通过·保证人** |
| 终止治理 | 纯预算 | 纯预算 | 段级对话上限 | 预算+平台期自适应 | 目标+迭代 | submit+成本上限 | — | 有界循环显式失败 | — | **准出冻结+预算+指纹熔断+证人优先** |
| 执行隔离 | JVM 安全管理器 | 子进程+建议 Docker | fork+建议 Docker | Docker 强制 | 推荐不强制 | 每实例 Docker | Docker harness | 进程组（弱） | mock+时间冻结 | **强制容器+预构建镜像+超时重建** |
| 持久化/台账 | CSV/HTML | CSV | 文件+计数 | 报告+复现包 | **SQLite 逐次台账** | 全量轨迹 | 评测数据集 | 断言台账+junit | 用例/mock YAML | **15 表 SQLite 全链路+RTM 快照** |
| 评估治理 | 覆盖统计 | 覆盖+最小化 | 覆盖+flaky 检测 | 覆盖时间线 | 覆盖+HTML | 外部基准 | 基准本身 | 自评（自报） | 比对+覆盖率 | **确定性指标+报告双闸回炉+盲测协议+n≥3+变异标定** |

---

## 5. 与本项目的异同

**同（谱系一致处，论文里写"继承"）**：
1. **覆盖反馈闭环**：本项目①缺口回灌 ↔ CoverUp 未覆盖段回灌 / qodo-cover 覆盖报告回灌——机制同构，差异在"预算内补测+准出冻结" vs "无限迭代追目标"。
2. **准入过滤哲学**：AST 闸 + collect-only 闸 + 定向剪枝 ↔ TestGen-LLM 三闸 / qodo-cover 逐测试 validate——同源（均防"不可构建/不可靠"用例入库）。
3. **固定图编排**：Hercules 1.0（2026-07）迁 LangGraph 并采用 planner/executor/assertion 状态机、planner 输出严格 JSON——与本项目"固定图骨架、LLM plan 是节点产物"选型独立趋同。
4. **台账硬否决**：Hercules "自称成功但台账有失败断言→强制改判" ↔ 本项目 "reject 仅推翻 pass、stop 不可被审查改判"——同一防造假语义的两种实现。
5. **容器隔离**：每实例沙箱与 SWE-agent/CodaMosa 一致；本项目差异点=面向 LLM 生成物的隔离与审计（对照 Testcontainers 面向依赖虚拟化，见 industry-testing-research.md §3.2）。

**异（差异化，论文里写"贡献"）**：
1. **需求追溯层独有**：plan 节点拆测试条件 + test_conditions 表 + rtm_maps 矩阵快照——八个对照项目全部没有"需求→测试"的可追溯结构（Hercules 的 Gherkin 只是输入契约，Keploy 的 spec 只是无追溯的输入）。
2. **预言纪律独有**：expected_basis 证据链 + 疑似产品缺陷证人冻结——直接回应 arXiv:2412.14137（LLM 生成器"为通过而设计断言"会反向验证 bug）；LLM 系两家实读提示词零防护，SBST 系回归式按构造跟随实现。
3. **缺陷如实上报**：SBST/Keploy 结构上不能报缺陷（回归 oracle 恒通过）；本项目 product_defect 停机并带证据链上报——这是"测试"与"回归录制"的本质分界。
4. **归因三分类 + 变异标定**：classify_failure（product/case/env）+ E4 混淆矩阵 14/14 标定——无任何对照项目对"归因质量"本身做过标定实验。
5. **终止治理有原则**：准出标准冻结 + 同指纹熔断（ISTQB "tests wear out" 原则背书）+ 证人优先于熔断/预算——对照 qodo-cover 的目标追逐空转烧迭代、SBST 的纯预算。
6. **评估治理内建**：评估数字全由确定性代码从 SQL 计算 + 报告双闸（规则先行 LLM 收窄）+ 叙述/审查/图表单快照同源 + 盲测语料协议 + 控制变量矩阵 + n≥3 预注册——对照项目评估=覆盖率数字，或外包给外部基准。
7. **修复语义**：YATE 式"带失败上下文定向重写坏用例、保留通过用例与证人"——对照回滚丢弃（qodo）、对话续聊（CoverUp，无 nodeid 粒度）、搜索淘汰（SBST 系）。

---

## 6. 优缺点

### 各对照项目
- **EvoSuite**：优=SBST 竞赛统治级战绩、大规模实证充分；缺=准维护（3 年无版本）、Java 8/11 绑定、回归 oracle 不能报缺陷、无需求维度。
- **Pynguin**：优=月度发版高度活跃、算法可插拔（含 LLM 变体）、断言变异分析筛选、文档化极佳；缺=自认 research prototype、无强沙箱、回归 oracle 固有局限。
- **CoverUp**：优=覆盖实测领先（80% vs 47%）、错误反馈消融证据、成本账诚实；缺=断言零防护、按段局部视角、无台账、仅 Python。
- **CodaMosa**：优=平台期检测+自适应熔断的开销控制干净、复现材料最全（代码+数据集+复现包）；缺=深度绑定已下线的 Codex、示例配置无断言生成、无需求维度。
- **qodo-cover**：优=工程成熟度（SQLite 逐次台账/录制回放/多语言/CI）、社区体量 5.7k★；缺=**已停止维护**、断言零防护、失败回滚丢弃弱于定向修复。
- **SWE-agent/mini**：优=ACI 接口设计的系统化验证、生态完整（基准/运行时/评测 CLI）、轨迹可回放；缺=判定外包、已转维护模式、无测试方法论。
- **SWT-bench**：优=证明测试生成可独立基准化、测试过滤修复候选的量化论据；缺=体量小（92★）、依赖 SWE-bench 生态。
- **Hercules**：优=Gherkin 一入口七类测试、断言台账硬否决、证据链完整、LangGraph 同路线；缺=隔离弱、性能声明无第三方佐证、AGPL 传染性。
- **Keploy**：优=语言无关零侵入、确定性回放+时间冻结、社区最活跃；缺=回归 oracle 固化历史行为、AI 能力闭源在云端、录制质量决定一切。

### 本项目
**优点**（对应 §5 异 7 条）：需求可追溯、预言有纪律、缺陷可如实上报、归因经变异标定、终止有原则、评估可审计可复现（答辩可现场从 SQL 重算每个数字）、修复保通过保证人。
**诚实短板**（论文"范围与限制"节素材）：
1. 单语言（Python）单机、微型被测对象——qodo 多语言、Keploy 协议级广度、SWE-agent 生态规模均远超本项目；
2. 无 CI 集成（qodo-cover 的主场）、不治理 flaky（v1 边界，人工标注通道已留）、每轮全量重跑（无回归选择，微型对象下合理）；
3. 语料规模小（9 缺陷 × n≥3），检出率差异是弱证据；单 LLM 供应商依赖（DeepSeek 402 余额不足两次中断实验，供应商风险实证）；
4. 静态闸 static_only 为占位、无属性测试、无 E2E/UI/安全维度（Hercules 覆盖七类）；
5. 单人工程 vs 社区级锤炼——工程健壮性未经生产流量检验（对抗测试 9 项+冒烟 15 项是当前上限）。

---

## 7. 适用场景对照

| 需求 | 首选 |
|---|---|
| Java/Python 仓库快速补单元覆盖（不问需求、不求报缺陷） | EvoSuite / Pynguin / CoverUp |
| 研究 LLM 与搜索的混合范式、预算感知设计 | CodaMosa |
| CI 流水线多语言增量补测（需自行 fork 维护） | qodo-cover |
| issue→补丁自动化研究、agent 基准评测 | SWE-agent / mini-swe-agent |
| 评测 agent 的测试生成/缺陷复现能力 | SWT-bench |
| QA 团队自然语言 E2E 回归 + 证据存档（UI/API/安全） | Hercules（注意 AGPL） |
| 遗留系统/微服务流量级回归、依赖虚拟化 | Keploy |
| **需求驱动、过程可审计、缺陷可如实上报、归因可信**的小中型 Python 仓库；教学/科研/测试流程治理 | **本项目** |

一句话定位：对照项目解决"**怎么把测试生成出来/跑起来**"，本项目在解决"**怎么让整个测试过程可信、可审计、可归因**"——两者是正交贡献，论文应避免声称在覆盖数字上超越 CoverUp（它们的目标函数就是覆盖率，本项目目标函数是过程治理）。

---

## 8. 引用纠错清单（写作前必读，全部经 2026-10-28 实查）

| # | 流传说法 | 核实结果 |
|---|---|---|
| 1 | CoverUp 仓库 sawrd/coverup（404） | **plasma-umass/coverup**；论文 FSE 2025（arXiv:2403.16218，DOI 10.1145/3729398） |
| 2 | CodaMosa = arXiv:2305.04115 | 该号是三值逻辑论文；**CodaMosa 无 arXiv 版**，正式出处 ICSE 2023 DOI 10.1109/ICSE48619.2023.00085；**代码已开源 microsoft/codamosa** |
| 3 | Pynguin 工具论文发表于 ISSTA 2022 | **ICSE Companion 2022 pp.168–172**（arXiv:2202.05218）；另 license 自 0.30.0 起为 MIT |
| 4 | EvoSuite 论文发表于 ICSE 2011 | **ESEC/FSE 2011**（DOI 10.1145/2025113.2025179）；现为准维护（最后 release 2021-09） |
| 5 | qodo-ai/cover-agent 活跃维护 | **2025-06-15 官宣停止维护**，改名 qodo-cover；引用须标注 |
| 6 | SWT-bench 仓库 eth-sri/swt-bench | 已 404，现址 **logic-star-ai/SWT-Bench** |
| 7 | SWE-agent 是当前主力 / Hercules "排名第一的开源测试 Agent" | SWE-agent 已转 **maintenance-only**（继任 mini-swe-agent）；Hercules 原话是 "world's first open-source testing agent"（首创声明），**性能声明无第三方佐证** |

---

## 9. 信源清单（全部 2026-10-28 实读）

| 项目 | 关键来源 |
|---|---|
| EvoSuite | github.com/EvoSuite/evosuite · evosuite.org（文档/论文页）· Properties.java 源码 |
| Pynguin | github.com/se2p/pynguin · pynguin.readthedocs.io（dev/overview、user/assertions、configuration 源码页） |
| CoverUp | github.com/plasma-umass/coverup · arxiv.org/abs/2403.16218（v4 全文）· doi.org/10.1145/3729398 |
| CodaMosa | github.com/microsoft/codamosa（codamosastrategy.py、outputfixers.py）· doi.org/10.1109/ICSE48619.2023.00085 · OpenAlex 元数据 |
| qodo-cover | github.com/qodo-ai/qodo-cover（README、docs/top_level_sequence_diagram.md、test_generation_prompt.toml、unit_test_db.py 均实读） |
| SWE-agent | github.com/SWE-agent/SWE-agent · swe-agent.com（aci/architecture/batch_mode/config）· swe-rex.com · github.com/SWE-agent/mini-swe-agent · arxiv.org/abs/2405.15793 |
| SWT-bench | github.com/logic-star-ai/SWT-Bench · arxiv.org/abs/2406.12952（v3 全文）· swtbench.com |
| Hercules | github.com/test-zeus-ai/testzeus-hercules（docs/architecture.md、releases）· testzeus.com/hercules |
| Keploy | github.com/keploy/keploy（AGENTS.md）· keploy.io/docs（introduction、generate-api-tests-using-ai） |

**未核验/低置信项（诚实标注）**：Hercules 各类性能声明（无方法学、无第三方）；mini-swe-agent ">74%" 为自报；SWT-bench 排行榜 2026-10 榜首成绩（DPIAgent 87.5% 等）来自榜单自述，未独立复核。
