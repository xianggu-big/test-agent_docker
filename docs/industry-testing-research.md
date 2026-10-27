# 软件测试主流流程与工程实践调研（面向 Test Agent 毕设的流程完备性对照）

> 生成日期：2026-10-27｜用途：毕设"相关工作/理论对齐"素材库 + 流程链路差距对照。
> 纪律：**全部关键断言经一手来源逐条核验**（Crossref DOI 元数据 / arXiv 官方 API 摘要 / 官方站点 HTTP 状态 / ISTQB 官方大纲 PDF 全文提取）。网络不可达项如实标注，不以讹传讹。

---

## 0. 执行摘要

本次调研覆盖六条主线：ISTQB v4.0 权威流程基线、测试分层策略（金字塔/四象限）、持续测试与容器化执行、协作式测试方法（验收标准/ATDD/BDD）、LLM 生成测试前沿谱系（含批判性证据）、质量度量与回归治理（变异测试/Oracle 问题/flaky）。

对 Test Agent 毕设的四个核心结论：

1. **项目的理论根基站得住**：ISTQB CTFL v4.0（2023-04-21 发布，v4.0.1 修订于 2024-09-15）官方大纲全文核验显示，项目已在用的概念——测试条件、准出标准（exit criteria，全文 25 处）、可追溯性（traceability，21 处）、用例优先级（§5.1.5）、测试金字塔（§5.1.6 已进大纲）——全部是官方正统术语[cite:1]。
2. **一处必须修正的理论章表述**：ISTQB 基础级 v4.0 全文 **"mutation" 0 次命中**——变异测试不在基础级设计技术清单中（四类技术=黑盒/白盒/经验/协作，经验类含错误推测法）[cite:1]。理论章"五法表"第五法应改写为"错误推测法"，变异测试单列为"质量度量/标定仪器"章节（以 Jia & Harman 变异测试综述为学术锚点[cite:13]）。项目 AGENTS.md 已有此纠偏意向（变异测试定位=归因标定仪器），本次核验为其提供了官方依据。
3. **项目机制在前沿文献中全部有对位，且部分设计是正向差异化**：五方法骨架↔ISTQB §4 四类技术[cite:1]；缺口回灌↔CoverUp（覆盖率反馈迭代提示，Python，比 CodaMosa 47% 提到 80%）[cite:4][cite:5]；定向修复/准入闸↔Meta TestGen-LLM 的 build/pass/coverage 三道准入过滤（真实数字：75% 可构建、57% 可靠通过、25% 提升覆盖、73% 推荐被工程师接受）[cite:3]；证人冻结/expected_basis↔对 LLM 测试生成器"为通过而设计断言"缺陷的批判性实证（该文证明这类工具会验证 bug、甚至拒绝暴露 bug 的用例）[cite:7]——**这批评论文必须进相关工作**，项目设计恰好是它的工程回应。
4. **发现一个低成本高回报的改进方向**：YATE（2025）实证"坏用例修复后价值高于直接丢弃"（比纯 LLM 生成多覆盖 32.06% 行、多杀 21.77% 变异体）[cite:6]——项目②定向修复当前是"剪除"，可升级为"带失败上下文重写该用例"，与证人冻结不冲突（仅作用于 case_defect 强信号类）。

---

## 1. 调研与核验方法（为什么可信）

信源分级采用 P0（原始/官方：标准文本、论文原文、官方文档）→ P1（权威二手：IEEE/ACM 收录、官方白皮书）→ P2（专业社区）的优先级。核验手段：

- **学术论文**：Crossref DOI 官方元数据（会议/期刊/作者/年份）+ arXiv 官方 API 摘要，双重核验。
- **标准与工具**：ISTQB 官方 v4.0.1 大纲 PDF（78 页）下载并全文文本提取，关键概念逐词计数定位；工具官网 HTTP 状态码 + 页面标题/描述抽取；PyPI 官方 JSON API 核验包存在性与版本号。
- **纠错实例（逐条核验必要性的直接证据）**：凭记忆写出的 TestGen-LLM arXiv 编号 2402.09191 经实测为另一篇无关论文（TCP 蜜罐主题），真实编号是 **2402.09171**[cite:3]。凭记忆引用文献必错，是本次调研的第一教训，也印证了"找到后重新识别正确性"这道工序的必要性。

两个**不可达项**（本网络环境阻断），处理为"不作为证据、只标注存在"：

- Google Testing Blog（testing.googleblog.com，含 flaky 测试系列名文）：连接层不可达。flaky 相关断言改由可核验的学术文献支撑[cite:27][cite:28][cite:29]。
- iso.org（ISO/IEC/IEEE 29119 系列标准页，返回 403）：**[unverified from this network]**。理论章引用 IEEE 829 作为"用例规格传统/历史出处"可保留，但论文定稿前应在校园网或 IEEE Xplore 复核其与 29119-3（测试文档部分）的承接关系并补注。

---

## 2. 权威流程基线：ISTQB CTFL v4.0 实测要点

官方事实（全部出自 PDF 原文提取）[cite:1]：

| 项目 | 核验结果 |
|---|---|
| 版本 | v4.0 于 2023-04-21 经 ISTQB 全体大会发布；v4.0.1 修订日期 2024-09-15，78 页 |
| 测试过程活动 | 测试计划 / 测试监控与控制 / 测试分析 / 测试设计 / 测试实现 / 测试执行 / 测试完成 |
| 第 4 章设计技术四类 | 黑盒（等价类划分 §4.2.1、边界值分析 §4.2.2、判定表 §4.2.3、状态迁移 §4.2.4）；白盒（语句测试/覆盖 §4.3.1、分支测试/覆盖 §4.3.2）；经验类（错误推测 §4.4.1、探索式测试 §4.4.2、检查表测试 §4.4.3）；**协作式（§4.5，v4 新增）**：协作式用户故事撰写 §4.5.1、验收标准 §4.5.2、ATDD §4.5.3，另提及 BDD/XP/FDD 等敏捷方法 |
| 第 5 章管理活动 | 测试计划 §5.1（含用例优先级 §5.1.5、**测试金字塔 §5.1.6、测试四象限 §5.1.7**）、风险与基于风险的测试 §5.2、测试监控/控制/完成 §5.3、配置管理、缺陷管理 |
| 关键词全文计数 | exit criteria 25 处；traceability 21 处；regression test 24 处；maintenance testing 11 处；**mutation 0 处；flaky 0 处** |
| 七测试原则之五 | "当心农药悖论"（Beware of the pesticide paradox）：重复执行同一批测试对新缺陷的检出力递减（正文表述即 tests wear out），概念源自 Beizer 1990——引用时用官方原则名，语义即"测试会磨损" |
| 考试结构 | 40 题、26 题及格 |

**对项目的意义**：

- 项目"四阶段（分析→设计→执行→评估）"是对官方七活动的可辩护简化：评估阶段吸收了"测试完成（含准出核对、完成报告）"，计划吸收进分析阶段。论文写作建议直接给映射表：analyze/plan↔分析、gen_tests↔设计+实现（官方把"实现"独立为活动，指测试脚本与数据的落地——恰好对应项目"用例即代码"）、run↔执行、report/collect_metrics↔完成。
- "准出标准冻结进任务行"是 exit criteria 的正统用法，答辩有官方背书。
- "Tests wear out" 原则是项目**指纹熔断 + 每轮再生成**机制的教科书级依据：同一批用例反复重跑价值递减，同指纹复现即判"原地打转"完全符合该原则的语义。
- 两个 v4 新增项项目未覆盖：**协作式方法（验收标准/ATDD/BDD）**与**测试四象限**（见 §5 建议）。
- flaky 与 mutation 均不在基础级大纲——flaky 属工业治理主题[cite:27][cite:28]，mutation 属高级/学术主题[cite:13]，项目的定位（标定仪器、未做 flaky 治理）与大纲边界一致，属诚实范围界定。

---

## 3. 分层与持续测试：金字塔、四象限、容器化执行

### 3.1 测试金字塔

Fowler 2012 年原文核心："底层的单元测试应远多于穿过 GUI 的高层广谱测试，以此构建均衡的自动化组合"[cite:2]。ISTQB v4.0 已将其纳入大纲正文（§5.1.6），与四象限（§5.1.7）、用例优先级（§5.1.5）并列[cite:1]。

**对项目**：毕设聚焦"单元测试为主 + 轻量集成"（docs/testing-theory.md §2 已声明），与金字塔基座层一致。论文建议补一句定位：Test Agent 自动化的正是金字塔中**人力最密集的基座层**，并引用大纲 §5.1.6 佐证该策略非自创。

### 3.2 Testcontainers：容器化测试的行业形态

官方定义（官网原文）："开源库，提供轻量、用后即抛的常见数据库、消息代理、浏览器等 Docker 容器实例"[cite:14]，支持 Java、Python、Go、.NET、Node.js、Rust、Ruby、PHP 等十二种语言生态；Python 版文档独立存在（testcontainers-python 2.0.0）[cite:15]。

**对项目的对照**（论文相关工作/L4 设计章素材）：

| 维度 | Testcontainers（行业） | 项目 L4 SandboxRunner |
|---|---|---|
| 目的 | 为被测代码提供真实依赖（DB/代理）的临时实例 | 为 LLM 生成的测试提供隔离、可复现的执行环境 |
| 粒度 | 按依赖服务起容器，测试结束即抛 | 整仓快照进常驻容器，exec 复用至任务结束 |
| 生命周期 | 框架托管（fixture 级） | 自研（任务级 + 超时重建 + 预清理） |

两者目的不同、不冲突：Testcontainers 解决"测试的外部依赖"，项目解决"测试本身的执行沙箱"。项目已有性能实验（容器热运行 0.8s vs 宿主 3.1s）证明常驻容器+exec 模式的开销可行。论文 L4 一节建议以 Testcontainers 为"容器技术在测试领域的既有应用"引用，凸显项目差异点（面向 LLM 生成物的隔离与审计，而非依赖虚拟化）[cite:14][cite:15]。

### 3.3 DORA 指标：持续测试的工程语境

DORA 交付效能指标体系以吞吐（变更前置时间、部署频率）与稳定性（失败部署恢复时间——由 MTTR 演化而来）为核心[cite:22]。持续测试的价值最终折算进这些交付指标（自动测试越快越稳，前置时间越短）。

**对项目**：毕设是单机系统、不接 CI 流水线，属诚实的范围界定。建议论文"未来工作"写明：R-Loop 若接入 CI（如 GitHub Actions 的失败重试门禁），其收益可用 DORA 口径度量（每次失败修复的引入时间）。不做实施承诺。

---

## 4. 协作式测试：用户故事、验收标准、ATDD/BDD（项目当前最大理论缺口之一）

ISTQB v4.0 新设协作式技术类[cite:1]：§4.5.1 协作式用户故事撰写、§4.5.2 验收标准、§4.5.3 ATDD（验收测试驱动开发），并在大纲中点名 BDD。BDD 的原始思想出处（Dan North《Introducing BDD》）页面可达[cite:21]；Gherkin/Given-When-Then 的工具化实现以 Cucumber 为代表，官网可达[cite:20]。

**对项目**：plan 节点拆出的"测试条件"（test_conditions 表）在语义上就是验收标准的雏形。两个低成本对齐动作：

1. **提示词格式对齐**：plan 的输出格式可加可选字段 `given/when/then`（或把 condition 写成 Given-When-Then 句式），使测试条件直接可追溯为"验收标准"——六要素中的"测试输入/操作步骤/预期结果"与 Given/When/Then 一一对应，理论映射表从此闭环（改动量：prompt_library.py 的 PLAN_SYSTEM 约 10 行）。
2. **理论章补一行**：协作式方法（ATDD/BDD）列入"相关理论"，说明 L1 界面的人工插用例+确认门即"协作式"的人机分工形态。

不建议引入完整 BDD 工具链（Cucumber 之于 Python 生态无必要）：项目的用例即代码路线已覆盖其可执行部分，引入 Gherkin 中间层反而破坏"用例↔断言先于执行"的直连性。

---

## 5. LLM 生成测试：前沿谱系与批判性证据（论文相关工作的核心素材）

按时间与思想谱系核验如下（全部经 arXiv API/Crossref 双核验）：

### 5.1 前置：搜索式的自动化测试生成（SBST）

- **EvoSuite**（Fraser & Arcuri，ESEC/FSE 2011，DOI 10.1145/2025113.2025179）：Java 自动单元测试生成的工业级基线工具[cite:9]。
- **Pynguin**（arXiv:2202.05218，2022）：Python 的 SBST 工具[cite:8]。

这两者是"LLM 之前"的自动化测试生成基线，论文相关工作按它们开场，可凸显 LLM 方法（语义理解、从需求直接生成）的增量。

### 5.2 混合与反馈驱动（项目的直接谱系）

- **CodaMosa**（ICSE 2023，DOI 10.1109/icse48619.2023.00085，Lemieux/Inala/Lahiri/Sen）：SBST 覆盖率停滞时引入 LLM 生成种子逃出平台期[cite:5]。
- **CoverUp**（arXiv:2403.16218，2024）：覆盖率分析 + 代码上下文 + 反馈进提示词，迭代引导 LLM 提高行/分支覆盖——每模块中位线+分支覆盖 **80%**（CodaMosa 47%）；整体 **89%**（对比 MuTAP 的 77%）[cite:4]。**项目①"覆盖缺口回灌"（term-missing 行注入 generate 提示词）与该文机制同构，引用时需说明差异：CoverUp 以覆盖率为目标函数做迭代，项目以"缺口行回灌 + 准出标准冻结"做预算内补测。**
- **Meta TestGen-LLM**（arXiv:2402.09171，2024，Alshahwan 等 9 人）：对**已有**人工测试做 LLM 增强改进，核心哲学是**准入过滤**——生成的测试类必须通过"可构建、可靠通过、覆盖率提升"等过滤器，以保证对原套件有可度量的改进，从而消除幻觉问题。实测数字（Instagram Reels/Stories）：**75% 可构建、57% 可靠通过、25% 提升覆盖率**；test-a-thon 中改进了 **11.5%** 的目标类，**73%** 的推荐被 Meta 工程师接受进入生产[cite:3]。**项目②"定向修复 + 两道闸（AST/collect-only）"与其准入思想同源；引用数字时以本节为准（73% 是"推荐接受率"，不是"通过率"，勿混用）。**

### 5.3 修复优先于丢弃（对项目②的改进证据）

- **YATE**（arXiv:2507.18316，2025）：对语法/语义错误的 LLM 生成测试，用"规则式静态分析 + 带上下文再提示"做**修复**而非丢弃，6 个开源项目实证：比纯 LLM 方法平均多覆盖 **32.06%** 行、多杀 **21.77%** 变异体；对比 HITS/SYMPROMPT/TestSpark/CoverUp 仍高约 22% 行覆盖[cite:6]。**项目②当前是"剪除坏用例"，YATE 证明坏用例往往是贴近程序逻辑的高价值种子——带失败上下文重写优于删除。**

### 5.4 批判性证据（项目防造假设计的学术对位，论文必引）

- **《Design choices made by LLM-based test generators prevent them from finding bugs》**（arXiv:2412.14137，2024）：以真实人写的带 bug 代码为输入评估 Codium CoverAgent 与 CoverUp，结论：**测试预言（oracle）为"通过"而设计的生成器，不仅可能测不出 bug，更会反向验证 bug（把错误行为接受为预期），并拒绝暴露 bug 的用例**[cite:7]。这从实证上支持了项目三条纪律：断言先于执行、expected_basis 标注预期来源、疑似产品缺陷的失败用例冻结为证人不回炉（vision-and-gaps §7.2）。
- **Oracle 问题的学术正名**：Barr 等人 IEEE TSE 2015 综述（DOI 10.1109/tse.2014.2372785）系统定义测试预言问题[cite:10]；Jahangirova 等 ISSTA 2017（DOI 10.1145/3092703.3098235）给出 oracle 分类与质量度量[cite:11]。项目 §7"自动归因不可能 100% 准（oracle 问题的本质）"这句话在论文里应挂这两篇，而非口语论证。

---

## 6. 质量度量与测试有效性（评估阶段的第二把尺子）

- **覆盖率的局限是共识**：CoverUp 以覆盖率为驱动目标并承认需配合其他手段[cite:4]；批判论文证明高覆盖率伴生"验证 bug"的风险[cite:7]。因此项目的准出标准（行覆盖 ≥ 冻结阈值）需要变异测试做交叉验证——**这正是项目阶段 4"变异标定实验"（注入已知缺陷测归因混淆矩阵）的意义**，论文表述可升级为"覆盖率度量测试做了什么，变异度量测试测出了什么"。
- **变异测试的学术与工具生态**（均核验存在）：经典综述 Jia & Harman（IEEE TSE 2011，DOI 10.1109/tse.2010.62）[cite:13]；Java 工业标杆 PIT（官网自述"JVM 上 state of the art 的变异测试系统、gold standard 覆盖率"）[cite:16]；JS/TS/C# 生态 Stryker[cite:17]；**Python 生态 mutmut**（官方文档自述"python mutation tester"）[cite:18]。项目标定实验的落地建议：**mutmut 起步**（pip 即装、与 pytest 直接集成、可在项目自己的容器内跑，与 L4 架构零冲突）；若需更强定制（变异算子、真值表）再考虑 cosmic-ray。PIT 作为"工业界 JVM 同类实践"在相关工作引用。
- **属性测试（Hypothesis，文档核验存在，版本 6.170.12）**[cite:19]：从性质/不变量出发生成输入，断言天然"先于执行"且与需求语义直接绑定。与 LLM 方法互补（Hypothesis 找输入、LLM 写预言）。项目定位为未来工作即可：在 gen_tests 提示词的五法骨架中加入"不变量"视角，或讨论"LLM 生成属性 + Hypothesis 生成输入"的混合路线。

---

## 7. 回归治理与 flaky（R-Loop 的行业对位）

- **回归测试三选**：Yoo & Harman 综述（STVR 2012，DOI 10.1002/stvr.430）：最小化、选择、优先化是回归测试缩减代价的三条经典路线[cite:12]。项目 R-Loop 每轮**全量重跑**——在毕设的微型被测对象上完全合理（热运行 0.8s），但论文应写明诚实边界；"选择"路线的现成实现是 pytest-testmon（PyPI 核验存在，v2.2.0，自述"selects tests affected by changed files and methods"）[cite:26]，列为未来工作。并行工具 pytest-xdist（v3.8.0）[cite:23]同理。
- **flaky 测试**（同一代码非确定性通过/失败）：开发者视角实证（21 名开发者分类 200 个已修复 flaky 用例，arXiv:1907.01466）[cite:27]；开发者需求综述（arXiv:2203.00483）[cite:28]；工业规模自动修复（FlakyGuard，arXiv:2511.14002，2025）[cite:29]。**与项目的精确关系**：
  - 项目的同指纹熔断（同失败模式重复 ≥2 次即停）语义上过滤的是"系统性重复失败"（Agent 原地打转），**不是** flaky（flaky 是忽过忽败、模式不重复）；
  - 边界情形：真 flaky 失败两轮出现同 nodeid 时，第一轮断言失败会被当 product_defect 冻结为"证人"——这是误报（flaky 伪证人）。论文诚实口径：**v1 不治理 flaky，数据层已留 feedback_annotations 人工标注通道（表已建，L1 标注 UI 待接）**；未来工作可接 pytest-randomly（v5.0.0，随机排序暴露顺序依赖）[cite:24]与 pytest-repeat（v0.9.4，重复执行检测）[cite:25]。
  - ISTQB 大纲 flaky 0 次命中（§2 核验），说明这是超出基础级成熟度的工业主题——项目不做它不是缺陷，但**论文要主动说破**，防止答辩被问"忽好忽坏怎么办"时无预案。

---

## 8. 对照项目的链路差距映射与落地建议

优先级：**P0 = 论文必需**（不做影响答辩与学术严谨）｜**P1 = 低成本高回报**（建议纳入剩余工作量）｜**P2 = 未来工作**（论文写清楚边界即可）。

| # | 主流实践 | 依据 | 项目现状 | 建议落点 | 级别 |
|---|---|---|---|---|---|
| 1 | ISTQB 基础级四类技术不含变异测试 | 大纲全文 mutation 0 处[cite:1] | 理论章五法表第五法=变异测试（prompt_library 用的是错误推测法，两者不一致） | 理论章五法表第五法改为"错误推测法"；变异测试移入"测试有效性度量"节并挂 Jia & Harman[cite:13] | **P0** |
| 2 | 准入过滤哲学（可构建/可靠通过/可度量改进） | TestGen-LLM 三闸与实测数字[cite:3] | 已有 AST 闸 + collect-only 闸 + 定向剪枝，缺"增量改进"度量 | 阶段 4 collect_metrics 落地时补一条：每轮报告"新增用例对覆盖率的增量贡献"（数据已在 test_reports.per_file） | P1 |
| 3 | 坏用例修复优先于丢弃 | YATE 实证 +32% 行覆盖[cite:6] | ②定向修复=按 nodeid 剪除 | 升级为"带失败上下文重写该用例"（只对 ImportError/SyntaxError 等强信号；断言失败证人不动，与 §7.2 不冲突） | P1 |
| 4 | 验收标准/ATDD/BDD 协作式方法 | ISTQB §4.5[cite:1]、Dan North[cite:21]、Cucumber[cite:20] | plan 节点测试条件无 Given-When-Then 结构 | PLAN_SYSTEM 输出格式加 given/when/then 字段 + 理论章映射表补一行 | P1 |
| 5 | 变异测试作有效性标定 | Jia & Harman[cite:13]、mutmut/PIT/Stryker 存在[cite:16][cite:17][cite:18] | 阶段 4 计划内（mutant 集+归因混淆矩阵） | 实验工具选型定为 **mutmut**（pip 即装、pytest 原生、容器内可跑） | **P0**（已在计划） |
| 6 | LLM 生成器的 oracle 陷阱批判 | arXiv:2412.14137 实证[cite:7] | expected_basis + 证人冻结已实现 | 论文相关工作**必引**该文，把项目三纪律定位为其工程回应 | **P0** |
| 7 | 测试预言问题学术定义 | Barr et al.[cite:10]、Jahangirova et al.[cite:11] | §7 归因设计引用为"oracle 问题"但无文献 | 归因设计节挂 DOI 引用 | **P0** |
| 8 | 覆盖率反馈驱动生成 | CoverUp[cite:4]、CodaMosa[cite:5] | ①缺口回灌已实现 | 论文写作时引用并写明差异（预算内补测 vs 无限迭代） | **P0**（写作） |
| 9 | "Tests wear out" 原则 | ISTQB 原则五（引 Beizer 1990）[cite:1] | 指纹熔断已实现 | 答辩讲稿引用该原则为熔断正名（一句话改动） | P1 |
| 10 | 回归测试选择/优先化 | Yoo & Harman[cite:12]、pytest-testmon[cite:26] | 每轮全量重跑 | 微型对象下写明诚实边界 + 未来工作挂 testmon | P2 |
| 11 | flaky 治理 | flaky 实证综述[cite:27][cite:28] | 无（指纹熔断语义≠flaky） | 论文范围界定写明"v1 不治理 flaky，伪证人由人工标注纠偏" + 未来工作 pytest-randomly/repeat[cite:24][cite:25] | P2 |
| 12 | 属性测试 | Hypothesis[cite:19] | 无 | 未来工作（LLM 生成属性 + Hypothesis 生成输入的混合路线） | P2 |
| 13 | 容器化测试的行业形态 | Testcontainers[cite:14][cite:15] | L4 自研 runner（差异：面向生成物隔离） | L4 设计章相关工作引用 + 差异表述 | P1（写作） |
| 14 | 交付效能口径 | DORA 指标[cite:22] | 单机系统，无 CI | 未来工作一句话（接入 CI 后按 DORA 口径度量收益） | P2 |
| 15 | 静态扫描工具链落地 | ruff[cite:32]/bandit[cite:30]/semgrep[cite:31] 均核验存在 | static_only 分级为占位（v1 未实现） | 论文把 static_only tier 写成"后续工作"，工具选型给 ruff（规范+缺陷）/bandit（安全）/semgrep（跨语言）三件套 | P2 |
| 16 | E2E 自动化（Playwright）等 | playwright.dev 核验存在[cite:33] | UI mockup 中置灰"规划中" | 维持范围界定不变，引用时注明工具真实存在、属扩展方向 | P2 |

**范围外声明**（论文"范围与限制"节素材，均有行业存在性佐证但本课题不承诺）：契约测试（微服务 API 验收）、性能/负载测试、安全渗透测试、可维护性等非功能测试维度（ISTQB 大纲将其列为测试类型而非本课题的自动化对象[cite:1]）。

---

## 9. 信源清单（全部经本次实测核验）

| # | 信源 | 定位 | URL / DOI | 核验方式与状态 |
|---|---|---|---|---|
| 1 | ISTQB CTFL v4.0.1 官方大纲（78 页 PDF） | 标准 P0 | https://istqb.org/certifications/certified-tester-foundation-level-ctfl-v4-0/（下载 id=3345） | HTTP 200 + PDF 下载 + 全文提取；v4.0 发布 2023-04-21，v4.0.1 2024-09-15 ✅ |
| 2 | Martin Fowler《Test Pyramid》(2012-05-01) | P0 | https://martinfowler.com/bliki/TestPyramid.html | HTTP 200，原文提取 ✅ |
| 3 | Alshahwan 等《Automated Unit Test Improvement using Large Language Models at Meta》 | 论文 P0 | arXiv:2402.09171 | arXiv API 摘要核验 ✅（注意：2402.09191 为无关论文，勿混） |
| 4 | 《CoverUp: Effective High Coverage Test Generation for Python》 | 论文 P0 | arXiv:2403.16218 | arXiv API 摘要核验 ✅ |
| 5 | Lemieux/Inala/Lahiri/Sen《CodaMosa》 | 论文 P0 | ICSE 2023, DOI 10.1109/icse48619.2023.00085 | Crossref 元数据核验 ✅ |
| 6 | 《YATE: The Role of Test Repair in LLM-Based Unit Test Generation》 | 论文 P0 | arXiv:2507.18316 | arXiv API 摘要核验 ✅ |
| 7 | 《Design choices made by LLM-based test generators prevent them from finding bugs》 | 论文 P0 | arXiv:2412.14137 | arXiv API 摘要核验 ✅ |
| 8 | 《Pynguin: Automated Unit Test Generation for Python》 | 论文 P0 | arXiv:2202.05218 | arXiv API 核验 ✅ |
| 9 | Fraser & Arcuri《EvoSuite》 | 论文 P0 | ESEC/FSE 2011, DOI 10.1145/2025113.2025179 | Crossref 元数据核验 ✅ |
| 10 | Barr/Harman/McMinn/Shahbaz/Yoo《The Oracle Problem in Software Testing: A Survey》 | 论文 P0 | IEEE TSE 2015, DOI 10.1109/tse.2014.2372785 | Crossref 元数据核验 ✅ |
| 11 | Jahangirova 等《Oracle problem in software testing》 | 论文 P0 | ISSTA 2017, DOI 10.1145/3092703.3098235 | Crossref 元数据核验 ✅ |
| 12 | Yoo & Harman《Regression testing minimization, selection and prioritization: a survey》 | 论文 P0 | STVR 2012, DOI 10.1002/stvr.430 | Crossref 元数据核验 ✅ |
| 13 | Jia & Harman《An Analysis and Survey of the Development of Mutation Testing》 | 论文 P0 | IEEE TSE 2011, DOI 10.1109/tse.2010.62 | Crossref 元数据核验 ✅ |
| 14 | Testcontainers 官网 | 工具 P0 | https://testcontainers.com/ | HTTP 200 + 官方描述/语言列表提取 ✅ |
| 15 | testcontainers-python 文档 (2.0.0) | 工具 P0 | https://testcontainers-python.readthedocs.io/ | HTTP 200 ✅ |
| 16 | PIT Mutation Testing | 工具 P0 | https://pitest.org/ | HTTP 200 + 官方描述提取 ✅ |
| 17 | Stryker Mutator | 工具 P0 | https://stryker-mutator.io/ | HTTP 200 ✅ |
| 18 | mutmut | 工具 P0 | https://mutmut.readthedocs.io/ | HTTP 200 ✅ |
| 19 | Hypothesis (6.170.12) | 工具 P0 | https://hypothesis.readthedocs.io/ | HTTP 200 ✅ |
| 20 | Cucumber 官网 | 工具 P0 | https://cucumber.io/ | HTTP 200 ✅ |
| 21 | Dan North《Introducing BDD》 | P0 | https://dannorth.net/blog/introducing-bdd/ | HTTP 200（旧 URL 重定向至该地址）✅ |
| 22 | DORA 四关键指标指南 | P0 | https://dora.dev/guides/dora-metrics-four-keys/ | HTTP 200 + 指标定义提取 ✅ |
| 23 | pytest-xdist (3.8.0) | 工具 P0 | https://pypi.org/pypi/pytest-xdist/json | PyPI API 核验 ✅ |
| 24 | pytest-randomly (5.0.0) | 工具 P0 | https://pypi.org/pypi/pytest-randomly/json | PyPI API 核验 ✅ |
| 25 | pytest-repeat (0.9.4) | 工具 P0 | https://pypi.org/pypi/pytest-repeat/json | PyPI API 核验 ✅ |
| 26 | pytest-testmon (2.2.0) | 工具 P0 | https://pypi.org/pypi/pytest-testmon/json | PyPI API 核验 ✅ |
| 27 | 《Understanding Flaky Tests: The Developer's Perspective》 | 论文 P0 | arXiv:1907.01466 | arXiv API 摘要核验 ✅ |
| 28 | 《A Survey on How Test Flakiness Affects Developers…》 | 论文 P0 | arXiv:2203.00483 | arXiv API 摘要核验 ✅ |
| 29 | 《FlakyGuard: Automatically Fixing Flaky Tests at Industry Scale》(2025) | 论文 P0 | arXiv:2511.14002 | arXiv API 检索核验 ✅ |
| 30 | bandit (1.9.4) | 工具 P0 | https://pypi.org/pypi/bandit/json | PyPI API 核验 ✅ |
| 31 | semgrep (1.182.1) | 工具 P0 | https://pypi.org/pypi/semgrep/json | PyPI API 核验 ✅ |
| 32 | Ruff 官方文档 | 工具 P0 | https://docs.astral.sh/ruff/ | HTTP 200 ✅ |
| 33 | Playwright 官网 | 工具 P0 | https://playwright.dev/ | HTTP 200 ✅ |

**未核验项（诚实标注）**：

- Google Testing Blog 系列（flaky 测试名文）：本网络连接层不可达，**未采用为证据**；flaky 断言改由 [cite:27][cite:28][cite:29] 支撑。若校园网可达，可作补充阅读。
- ISO/IEC/IEEE 29119 系列与 IEEE 829 的承接关系：iso.org 返回 403，**[unverified from this network]**；论文引用 IEEE 829 作为历史出处（六要素传统）可保留，现行标准建议定稿前在 IEEE Xplore 复核。
- Kent C. Dodds "Testing Trophy"：原直链 404（博客站点迁移），仅核实其博客标签存在。**[unverified]**，金字塔（[cite:2] + 大纲 §5.1.6[cite:1]）已足够支撑分层论证，不依赖 trophy。
