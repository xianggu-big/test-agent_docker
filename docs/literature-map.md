# 论文逐模块文献地图（literature-map）

> 生成日期：2026-10-28｜用途：论文写作时「某章该引谁、引它说什么」的速查地图。
> 纪律：**每个编号/DOI 均经实测核验**（arXiv API 官方接口返回的标题/作者/年份，或 Crossref 元数据，或官方站点 HTTP 200）。
> 本次核验纠出 **3 个此前凭记忆写错的编号**（见 §5 纠错记录）——引用前逐条核验是铁律，本项目已有两次前科
> （TestGen-LLM 2402.09191→2402.09171；本轮 GEM/SWT-bench/对抗双 agent）。
> 与 docs/industry-testing-research.md 的关系：那份是 33 信源核验底稿（生成谱系/oracle/变异/flaky/工具链已覆盖），
> 本文不重复展开，只做：①总图（章节→文献→用法）②本次新增文献 ③纠错与未核验清单。

## 0. 核验状态标记

| 标记 | 含义 |
|---|---|
| ✅arXiv | arXiv API 实测返回该编号的标题/第一作者/年份 |
| ✅Crossref | Crossref 元数据实测（DOI→标题/出版物） |
| ✅官方 | 官方站点/文档 HTTP 200 或官方 API |
| ⚠️ | 本次未能权威核验，**引用前必须人工确认**（清单见 §6） |

会议 venue（NeurIPS/ICLR 等）未逐一二次核验出处，标注依据为论文页与检索结果；定稿前用 Crossref/DBLP 复核一遍即可（与 ISO 29119 同等处理）。

---

## 1. 总图：论文章节 × 核心文献

| 论文章节 | 必引（P0） | 增强（P1） | 作用一句话 |
|---|---|---|---|
| 1 绪论 / 相关工作总起 | ISTQB v4.0[cite:旧1]、2307.07221 综述 | 2412.14137 批判 | 领域有标准与综述，LLM 测试是活跃前沿但有已知缺陷 → 本课题动机 |
| 2 理论基础 | ISTQB、Barr oracle 综述、Gotel&Finkelstein | Fowler 金字塔、Chen 蜕变综述 | 四阶段流程、oracle 问题、RTM、分层策略的正统出处 |
| 3 系统设计 L1-L5 | Testcontainers、Merkel、Felter | Toolformer、MCP 官方 | L4 选型量化依据、L3 工具注册的谱系定位 |
| 4 关键机制（R-Loop/归因/评估/审查） | CoverUp、YATE、TestGen-LLM、2412.14137、MT-Bench | CodaMosa、Reflexion、Self-Refine | 每个机制都有学术对位，且写明与对位工作的差异 |
| 5 实验（E1-E5） | Jia&Harman、mutmut、Felter | SWT-bench、2603.23443 | 变异标定方法学、容器性能基线、语料来源多元 |
| 6 展望 | MutGen(ACH)、SWT-bench | PBT 三篇、蜕变、MCP 生态数据 | 每条未来工作挂一个已核验的锚点 |

---

## 2. 按系统模块的详细映射

### 2.1 L2 编排骨架（固定 LangGraph 图 + LLM 节点产物）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| ReAct: Synergizing Reasoning and Acting in Language Models（Yao, ICLR 2023） | arXiv:2210.03629 | ✅arXiv | Agent 范式起点；本项目与之差异：**不靠自由 ReAct 循环，用固定图骨架保证可复现可画图**，LLM 的 plan 是节点产物不是流程控制 |
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering（Yang 等，NeurIPS 2024） | arXiv:2405.15793 | ✅arXiv | 代表"给 agent 自由操作接口"路线；本项目是它的反面设计（受约束的测试域 agent），相关工作对比用 |
| SWE-bench: Can Language Models Resolve Real-World GitHub Issues?（Jimenez, ICLR 2024） | arXiv:2310.06770 | ✅arXiv | "repo 级 LLM 任务"的基准化先例；本项目语料方法学（issue→测试）与之同向，但对象是测试生成不是修 bug |
| Reflexion: Language Agents with Verbal Reinforcement Learning（Shinn, NeurIPS 2023） | arXiv:2303.11366 | ✅arXiv | R-Loop 的 agent 谱系远亲：以语言反馈迭代；本项目差异是**失败归因驱动的定向迭代 + 三停语义**，不是无界重试 |
| Self-Refine: Iterative Refinement with Self-Feedback（Madaan, NeurIPS 2023） | arXiv:2303.17651 | ✅arXiv | "LLM 自审自改"的谱系；本项目 review 节点与之差异：**规则闸先行、LLM 闸只收窄聚焦、reject 不回炉**（run46 死循环实证） |
| LangGraph 官方文档 | requirements 中 langgraph 包 | ✅工程引用 | 编排框架的工程引用，无需学术出处 |
| Software Testing with Large Language Models: Survey, Landscape, and Vision（Wang 等，IEEE TSE） | arXiv:2307.07221 | ✅arXiv | 相关工作总起框架：按测试活动分类 LLM 用法，本项目落在其中的 unit test generation + test oracle 两格 |

### 2.2 analyze + plan/RTM（测试分析阶段）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| ISTQB CTFL v4.0.1 大纲（测试条件/traceability/exit criteria） | istqb.org（旧底稿 cite:1） | ✅官方（旧核验） | 测试条件与 RTM 的标准术语出处；X3 实验证明关掉 plan/RTM recall 0.4→0.2——"测试分析阶段提升检出能力"的控制变量证据 |
| Gotel & Finkelstein: An Analysis of the Requirements Traceability Problem（ICRE 1994） | DOI 10.1109/ICRE.1994.292398 | ✅Crossref | **RTM 的开山文献**：pre-/post-traceability 定义。plan 节点+rtm_maps 表是它的自动化实现；引这一篇，RTM 就不是"自造概念" |
| Dan North: Introducing BDD / Cucumber / ISTQB §4.5 协作式方法 | dannorth.net / cucumber.io / 大纲（旧底稿 cite:20/21） | ✅官方（旧核验） | 六要素（输入/步骤/预期）与 Given-When-Then 验收标准的对应；plan 拆条件=验收标准的雏形 |
| IEEE 829 用例规格传统 | 旧底稿 §2 | ⚠️（iso.org 不可达，旧核验已标注） | 六要素历史出处；定稿前在 IEEE Xplore 复核与 29119-3 的承接 |

### 2.3 gen_tests（LLM 用例生成谱系 + 方法骨架）

> 谱系主线（全部旧核验，详见 industry-testing-research.md §5）：EvoSuite/Pynguin（SBST 基线）→ CodaMosa（LLM 破覆盖率平台期）→ CoverUp（覆盖反馈迭代）→ TestGen-LLM（准入过滤）→ YATE（修复优于丢弃）→ 2412.14137（批判：生成器为通过而设计断言）。本项目机制与它们的对位关系旧底稿已写全，此处只补**本次新增**的方法骨架扩容文献：

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| Etna: An Evaluation Platform for Property-Based Testing（Keles/Shi/Goldstein 等，UPenn） | arXiv:2603.27002 | ✅arXiv | PBT 有效性的评估平台；"方法骨架七法扩容（+属性测试）"的实证支撑 |
| Understanding the Characteristics of LLM-Generated Property-Based Tests in Exploring Edge Cases（Tanaka 等） | arXiv:2510.25297 | ✅arXiv | **LLM 生成 PBT 的边界探索特性**——与本项目 V6 边界攻击开关直接相关：LLM 不主动边界攻击的实证 |
| LLM-based Property-based Test Generation for Guardrailing Cyber-Physical Systems（Etemadi） | arXiv:2505.23549 | ✅arXiv | LLM+PBT 已有完整工作链的证据；"属性先于执行确定"与 expected_basis 纪律兼容 |
| Chen 等：Metamorphic Testing: A Review of Challenges and Opportunities（ACM Computing Surveys 2018） | DOI 10.1145/3143561 | ✅Crossref | **蜕变测试正统综述**："无 oracle 函数让 LLM 提炼蜕变关系"答辩答案的学术挂靠点（注意：不是 LLMORPH，见 §5） |
| Hypothesis（工具，旧核验 cite:19） | hypothesis.readthedocs.io | ✅官方（旧核验） | 七法落地工具；"LLM 生成属性 + Hypothesis 生成输入"混合路线 |

### 2.4 三道闸与 review 节点（生成物审查 / 报告二次审查）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena（Zheng, NeurIPS 2023） | arXiv:2306.05685 | ✅arXiv | LLM 审查的可靠性与偏置（位置/冗长/自偏好）的权威出处；支撑本项目设计：**规则闸先行、LLM 闸收窄聚焦、审查不看生成提示词** |
| TestGen-LLM 准入过滤（旧核验 cite:3） | arXiv:2402.09171 | ✅arXiv（复验） | 静态闸/collect-only 闸/"可度量改进"哲学的出处 |
| 2412.14137（旧核验 cite:7） | arXiv:2412.14137 | ✅arXiv（复验） | 生成用例"为通过而设计断言"的批判实证；三道闸与 expected_basis 的动机文献，**必引** |
| Investigating Test Overfitting on SWE-bench（Ahmed） | arXiv:2511.16858 | ✅arXiv | "迁就测试通过而不修 bug"的最新实证（2025-11）；review 双闸防造假叙事的补强证据 |

### 2.5 run + R-Loop（执行 / 回炉 / 熔断）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| ISTQB 原则五 tests wear out（旧核验 cite:1） | 大纲正文（概念源自 Beizer 1990） | ✅官方（旧核验） | 同指纹熔断的教科书依据：重复测试对同一缺陷检出力递减 |
| YATE（旧核验 cite:6） | arXiv:2507.18316 | ✅arXiv（复验） | 定向修复 rewrite 链路（E2 链路 B）的直接出处 |
| CoverUp（旧核验 cite:4） | arXiv:2403.16218 | ✅arXiv（复验） | 缺口回灌机制同构文献；差异=预算内补测 vs 无限迭代 |
| Yoo & Harman 回归综述（旧核验 cite:12） | DOI 10.1002/stvr.430 | ✅Crossref（旧核验） | 每轮全量重跑的诚实边界 + 未来工作（testmon 选择性重跑） |
| flaky 三篇（旧核验 cite:27/28/29） | 1907.01466 / 2203.00483 / 2511.14002 | ✅arXiv（复验） | "指纹熔断≠flaky 治理"的范围界定；v1 不治理 flaky 的主动声明素材 |

### 2.6 归因三分类（oracle 问题）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| Barr 等：The Oracle Problem in Software Testing: A Survey（IEEE TSE 2015，旧核验 cite:10） | DOI 10.1109/tse.2014.2372785 | ✅Crossref（旧核验） | "自动归因不可能 100% 准"的学术正名 |
| Jahangirova 等 oracle 分类（ISSTA 2017，旧核验 cite:11） | DOI 10.1145/3092703.3098235 | ✅Crossref（旧核验） | oracle 质量度量；E4 混淆矩阵的方法学近亲 |
| Can we find bugs using LLM-generated oracles?（Konstantinou 等；即"actual or expected behaviour"研究） | arXiv:2410.21136 | ✅arXiv | **actual vs expected 行为之辨**——expected_basis 字段（预期值来源显式化）的直接学术对位 |
| Do LLMs Generate Useful Test Oracles? An Empirical Study（Molinelli 等，UW / Michael Ernst 组，ASE 2025） | homes.cs.washington.edu/~mernst/pubs/neurosymbolic-oracles-ase2025.pdf | ✅官方 PDF（网络检索定位） | 13,866 个 oracle 的实证；断言质量分析小节（P2-F）的现成叙述搭档 |
| 2412.14137（同上） | arXiv:2412.14137 | ✅arXiv | 生成器会反向验证 bug → "宁可误报不可造假"归因纪律的实证支撑 |

### 2.7 评估五工具与实验（E1-E5）

| 实验 | 核心文献 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| E1 容器 vs 宿主 | Felter 等：An Updated Performance Comparison of Virtual Machines and Linux Containers（IBM） | DOI 10.1109/ISPASS.2015.7095802 ✅Crossref（注意：常被引作 ISPASS 2014 会议版，Crossref 收录的是 2015 卷） | 容器近原生性能的 IBM 经典测量；E1 的 3.8x 数字有了同类研究对位（他们测通用负载，本项目测 pytest 负载） |
| E1（环境一致性侧） | Merkel: Docker: Lightweight Linux Containers for Consistent Development and Deployment（Linux Journal 2014） | linuxjournal.com HTTP 200 ✅官方 | "环境即镜像、工具链版本锁死→实验可复现"的 Docker 原始文献 |
| E2 三链路 | YATE（B 链路）+ CoverUp（A 链路缺口思想） | ✅arXiv | 三链路对比就是 YATE 思想与 prune 基线、hybrid 合成的受控实验 |
| E4 变异标定 | Jia & Harman 变异综述（旧 cite:13）+ mutmut（旧 cite:18）+ **MutGen: Mutation-Guided LLM-based Test Generation at Meta（Foster/Harman 等）** | DOI 10.1109/tse.2010.62 ✅Crossref（旧）；arXiv:2501.12862 ✅arXiv（本次纠偏：该文方法名是 **MutGen**） | 变异测试方法学 + 生产级变异引导生成的最新对标（ACH/MutGen 是 TestGen-LLM 官方续作） |
| E4（对位工作） | Test vs Mutant: Adversarial LLM Agents for Robust Unit Test Generation（Chang） | arXiv:2602.08146 ✅arXiv（本次纠偏：旧文档写 2505.15253 是错的） | "用变异体作为生成器的对抗陪练"——E4 仪器若升级为生成信号（复盘 §5-③），这是最新对位 |
| E5 审查双闸 | MT-Bench（LLM judge 偏置）+ 2511.16858（overfitting 动机） | ✅arXiv | 规则闸先行的必要性 = judge 有偏置；拦截数字造假的动机 = 迁就性叙述真实存在 |
| 语料方法学 | SWT-Bench: Testing and Validating Real-World Bug-Fixes with Code Agents（Mündler，NeurIPS 2024 D&B） | arXiv:2406.12952 ✅arXiv（本次纠偏：旧编号 2310.12576 是数学论文） | "真实补丁→验证测试"基准；语料 v3 从中抽样的可行性依据；另有 Multi-SWT-Bench（2609.34752，多语言版） |
| 语料方法学 | Evaluating LLM-Based Test Generation Under Software Evolution（Haroon） | arXiv:2603.23443 ✅arXiv | 演进回归锚点（复盘 §5-⑥）；"一次性系统"走向"持续系统"的展望挂靠 |

### 2.8 L3 工具注册表 / MCP（相关工作 + 未来工作）

| 文献 | 编号/出处 | 核验 | 引用/借鉴话术 |
|---|---|---|---|
| Toolformer: Language Models Can Teach Themselves to Use Tools（Schick, NeurIPS 2023） | arXiv:2302.04761 | ✅arXiv | "LLM 学会用工具"的学术起点；L3 注册表的谱系定位 |
| Model Context Protocol 官方规范（2026-07-28 版：无状态核心/Apps/Tasks/弃用策略） | modelcontextprotocol.io 及 spec 页 | ✅网络调研（2026-10-28，信源见对话留痕：VentureBeat/NashTech/WorkOS） | L3 与 MCP 概念同构但选进程内实现（审计台账+单快照语义锁死在确定性代码里）；"评估五工具封装为 MCP server"列未来工作 |
| MCP 生态与安全现状 | Practical DevSecOps 2026 报告、MCPProxy 审计（2026-10-28 网络调研） | ✅网络调研（P2 级信源） | 展望素材：MCP server 是新一代大规模第三方软件工件、安全成熟度严重滞后 → "用本系统测 MCP"是天然延伸（详见 2026-10-28 会话分析） |

### 2.9 L4 容器沙箱

Testcontainers（旧 cite:14/15，✅官方）｜差异表述：行业方案解决"测试的外部依赖"，本项目解决"LLM 生成物本身的隔离与审计"。Felter/Merkel 见 §2.7（E1）。

### 2.10 flaky 与回归治理（诚实边界素材）

1907.01466 / 2203.00483 / 2511.14002（✅arXiv 复验）+ pytest-testmon/xdist/randomly/repeat（旧 cite:23-26，✅PyPI API）——v1 不治理 flaky、每轮全量重跑的主动声明与未来工作，全部素材在旧底稿 §7，此处不重复。

---

## 3. 本次新增文献核验明细（arXiv API / Crossref 实测于 2026-10-28）

| # | 文献短名 | 编号/DOI | 核验方式 | 实测返回（标题前缀+第一作者） |
|---|---|---|---|---|
| 1 | ReAct | 2210.03629 | ✅arXiv | ReAct: Synergizing Reasoning and Acting… / Shunyu Yao |
| 2 | Reflexion | 2303.11366 | ✅arXiv | Reflexion: Language Agents… / Noah Shinn |
| 3 | Self-Refine | 2303.17651 | ✅arXiv | Self-Refine: Iterative Refinement… / Aman Madaan |
| 4 | LLM-as-a-Judge | 2306.05685 | ✅arXiv | Judging LLM-as-a-Judge with MT-Bench… / Lianmin Zheng |
| 5 | Toolformer | 2302.04761 | ✅arXiv | Toolformer… / Timo Schick |
| 6 | SWE-bench | 2310.06770 | ✅arXiv | SWE-bench: Can Language Models Resolve… / Carlos E. Jimenez |
| 7 | SWE-agent | 2405.15793 | ✅网络检索（arXiv 摘要页） | SWE-agent: Agent-Computer Interfaces… / John Yang |
| 8 | SWT-Bench | 2406.12952 | ✅arXiv | SWT-Bench: Testing and Validating Real-World Bug-Fixes… / Niels Mündler |
| 9 | LLM 测试综述 | 2307.07221 | ✅arXiv | Software Testing with Large Language Models… / Junjie Wang |
| 10 | Meta MutGen（原 ACH） | 2501.12862 | ✅arXiv | Mutation-Guided LLM-based Test Generation at Meta / Christopher Foster |
| 11 | Test Overfitting | 2511.16858 | ✅arXiv | Investigating Test Overfitting on SWE-bench / Toufique Ahmed |
| 12 | Test vs Mutant | 2602.08146 | ✅arXiv | Test vs Mutant: Adversarial LLM Agents… / Pengyu Chang |
| 13 | 软件演进 LLM 测试 | 2603.23443 | ✅arXiv | Evaluating LLM-Based Test Generation Under Software Evolution / Sabaat Haroon |
| 14 | LLM oracle actual/expected | 2410.21136 | ✅arXiv | Can we find bugs using LLM-generated oracles? / Michael Konstantinou |
| 15 | UW oracle 实证（ASE 2025） | 无 arXiv；Ernst 组官网 PDF | ✅官方 PDF | Do LLMs Generate Useful Test Oracles? / Molinelli 等 |
| 16 | Etna（PBT 评估平台） | 2603.27002 | ✅arXiv | Etna: An evaluation platform for property-based testing / Alperen Keles |
| 17 | LLM-PBT 边界特征 | 2510.25297 | ✅arXiv | Understanding the Characteristics of LLM-Generated Property-Based Tests… / Hidetake Tanaka |
| 18 | LLM-PBT CPS | 2505.23549 | ✅arXiv | LLM-based Property-based Test Generation for Guardrailing… / Khashayar Etemadi |
| 19 | LLMORPH（ASE 2025） | 2603.23611 / DOI 10.1109/ASE63991.2025.00385 | ✅arXiv+✅Crossref | LLMorph: Automated Metamorphic Testing of Large Language Models / S. Cho |
| 20 | Chen 蜕变综述（CSUR 2018） | DOI 10.1145/3143561 | ✅Crossref | Metamorphic Testing / ACM Computing Surveys |
| 21 | Gotel & Finkelstein（ICRE 1994） | DOI 10.1109/ICRE.1994.292398 | ✅Crossref | An analysis of the requirements traceability problem |
| 22 | Felter 容器性能（IBM） | DOI 10.1109/ISPASS.2015.7095802 | ✅Crossref（bibliographic 检索） | An updated performance comparison of virtual machines and Linux containers |
| 23 | Merkel Docker（Linux Journal 2014） | linuxjournal.com 文章页 | ✅官方 HTTP 200 | Docker: Lightweight Linux Containers… |
| 24 | 既有 13 篇复验（TestGen-LLM/CoverUp/YATE/2412.14137/Pynguin/flaky×3/PBT 邻接等） | 见旧底稿 §9 | ✅arXiv（本次批量复验全中） | 标题/作者与旧核验一致 |

---

## 4. 会议 venue 附注（未逐一二次核验，定稿前 DBLP/Crossref 复核）

ReAct=ICLR 2023；Reflexion/Self-Refine/Toolformer/MT-Bench=NeurIPS 2023；SWE-bench=ICLR 2024；SWE-agent=NeurIPS 2024；SWT-Bench=NeurIPS 2024（Datasets & Benchmarks）；2307.07221=IEEE TSE；2406.12952 与 2602.08146 的正式 venue 以最新论文页为准。

---

## 5. 错误编号纠错记录（重要——旧文档已同步修正）

| 旧编号/旧口径 | 实测真相 | 处理 |
|---|---|---|
| GEM = arXiv:2408.13156（AGENTS.md、模块复盘 §5-③） | 2408.13156 是空间物理论文（chorus emissions）；GEM 论文本身无 arXiv/DOI 权威定位 | 从"可引用"降级为 ⚠️；变异反馈生成的已核验替代锚点 = **MutGen 2501.12862** + **Test vs Mutant 2602.08146** |
| Test vs Mutant = 2505.15253（模块复盘 §5-③） | 2505.15253 是 Hawkes 过程数学论文；真身 = **2602.08146**（已核验） | 已纠错 |
| SWT-bench = 2310.12576（记忆候选） | 2310.12576 是椭圆偏微分方程数学论文；真身 = **2406.12952**（已核验） | 已纠错 |
| LLMORPH = "LLM 生成蜕变关系测程序"（模块复盘 §5-②） | LLMORPH 实为**用蜕变测试测 LLM 系统**（ASE 2025 tool demo，方向相反） | 口径纠偏；测无 oracle 程序的蜕变正统锚点 = **Chen CSUR 2018（10.1145/3143561）**，LLMORPH 改引为"MT 在 LLM 系统测试的应用" |
| "OOPSLA 2025 实证属性测试杀 ~50× 变异体"（模块复盘 §5-①、AGENTS.md） | arXiv 标题检索 + 网络检索均未能定位该出处 | "50×"数字在人工找到原文前**不得进论文**；PBT 有效性的已核验替代 = Etna 2603.27002 + Tanaka 2510.25297 |

教训重申（与旧底稿 §1 纠错实例同源）：**凭记忆写编号必错**。本次 23 个编号批量过 API，3 个记忆编号全错、0 个已核验编号出错。

---

## 6. 未核验项清单（引用前必须人工确认）

1. **GEM**（"变异反馈强化 LLM 断言"框架）：仅网络检索线索（Semantic Scholar 条目 + CIBSE 会议 PDF），无权威定位。要么人工找到原文，要么论文中只用 MutGen/Test vs Mutant 表述该思想。
2. **"PBT 杀变异 ~50×"**：出处不明（疑似 2023 年某实证研究，未定位）。要么找到原文，要么删数字，用 Etna/Tanaka 的已核验结论替代表述。
3. **IEEE 829 ↔ ISO 29119-3 承接关系**：iso.org 403（旧底稿已标注），定稿前校园网/IEEE Xplore 复核。
4. 本表各条目的**会议 venue**（§4 所列）：DBLP 一遍复核，5 分钟工作量。
5. MCP 生态数字（registry 4.7 万 server、SDK 月下载、安全分）：来自 2026-10-28 网络调研（P2 级信源），进论文需换算成当时可复核的一手来源或标注"截至 2026-10 的行业报告口径"。
6. **radon/PICT/Hypothesis-target()**（方法组合设计引入的工具与技法）：用前人工核验（见 docs/黑盒白盒方法组合与沙箱自适应设计.md §9）。

---

## 7. 方法组合设计新增文献（2026-10-29，Crossref bibliographic 检索实测）

> 服务于 docs/黑盒白盒方法组合与沙箱自适应设计.md 的方法出处。本轮再次实锤"记忆 DOI 必错"：8 条中 3 条记忆值错误（Rapps-Weyuker 232177→232226、Chow 231384→231496、Felter 式教训再现），检索 8/8 全中。

| # | 文献 | DOI（✅Crossref） | 方案落点 |
|---|---|---|---|
| 25 | McCabe《A Complexity Measure》 | 10.1109/TSE.1976.233837 | 白盒-基路径/圈复杂度（W4） |
| 26 | Rapps & Weyuker《Selecting Software Test Data Using Data Flow Information》 | 10.1109/TSE.1985.232226 | 白盒-数据流（W7，未来工作） |
| 27 | Chow《Testing Software Design Modeled by Finite-State Machines》 | 10.1109/TSE.1978.231496 | 黑盒-状态迁移 N-switch（B4） |
| 28 | Claessen & Hughes《QuickCheck》 | 10.1145/351240.351266（ICFP 2000；再版 10.1145/1988042.1988046） | 黑盒-属性测试谱系起点（B7） |
| 29 | Godefroid/Klarlund/Sen《DART: Directed Automated Random Testing》 | 10.1145/1065010.1065036（PLDI 2005） | 黑盒-随机测试谱系（B6） |
| 30 | Manès 等《The Art, Science, and Engineering of Fuzzing: A Survey》 | 10.1109/TSE.2019.2946563（TSE） | 白盒-模糊测试边界条目（W8） |
| 31 | Ostrand & Balcer《The category-partition method for specifying and generating functional tests》 | 10.1145/62959.62964（CACM 1988） | 黑盒-等价类的学术形式化（B1） |
| 32 | Kuhn 等《Software fault interactions and implications for software testing》 | 10.1109/TSE.2004.24 | 黑盒-组合测试实证（B9） |

书籍类（无 DOI 需核，出版社+年份准确即可）：Myers《The Art of Software Testing》1979（B2）、Beizer 1990（tests wear out 概念源）、Winters 等《Software Engineering at Google》O'Reilly 2020（大厂维度）。

---

## 8. 四级双轨方向借鉴清单核验（2026-10-29，arXiv API + Crossref + GitHub API 实测）

> 服务于 docs/四级双轨测试框架研究规划.md §6 借鉴策略清单的 ⚠️ 项清零（§10.4 待办兑现）。
> 背景：用户提供的 DeepSeek 生成框架清单 10 条（Schemathesis/Hypothesis/SAINT/AutoRestTest/MASTEST/Keploy/Pynguin/EvoSuite/ChatUniTest/Agent Q±Langfuse）全数核验——**无一幻觉，但纠出 4 处引用级错误**（见 §8 纠错行）。star 数为 2026-10-29 快照。

| # | 文献/工具 | 编号/DOI/仓库（核验方式） | 实测要点与落点 |
|---|---|---|---|
| 33 | Schemathesis | github.com/schemathesis/schemathesis（✅GitHub API：3729★，v4.32.7 2026-10-27，当天有 push，OS Independent） | 基于 Hypothesis 的 OpenAPI 属性化测试：schema 生成随机+边界+畸形输入、契约断言 `call_and_validate()`、500 检出、状态化工作流；支持 OpenAPI 2.0—3.2+GraphQL。L2 契约黑盒生成器思路挂靠（自研 skill 保审计台账一致，不嵌其 CLI） |
| 34 | SAINT（Pan 等，Georgia Tech 等） | arXiv:2511.13305 + DOI 10.1145/3744916.3773185（✅arXiv+✅Crossref，ICSE 2026） | 服务级**白盒**集成测试：静态分析构建端点模型+操作依赖图，LLM Agent planning/action/reflection 循环合成端点测试与场景测试，企业 Java 应用实测。L2 白盒轨（调用链/顺序约束）设计思路直接挂靠 |
| 35 | AutoRestTest（Kim/Stennett/Sinha/Orso，Georgia Tech） | 主会 arXiv:2411.07098 + DOI 10.1109/ICSE55347.2025.00179；Demo 轨 arXiv:2501.08600（✅arXiv+✅Crossref） | **标题陷阱**：ICSE 2025 主会论文标题是 "A Multi-Agent Approach for REST API Testing with Semantic Graphs and LLM-Driven Inputs"，AutoRestTest 是文中**工具名**；标题以 AutoRestTest 开头的是 ICSE-Companion 2025 Demo 短文——引用时按需二选一，勿把主会论文标题写成 AutoRestTest。SPDG 语义属性依赖图+多 Agent 强化学习+LLM 输入。L2 黑盒轨多 Agent 分工叙事（只借提示词分工，不借多 Agent 框架，D9 口径） |
| 36 | MASTEST（Han & Zhu） | arXiv:2511.18038 + DOI 10.1109/AITest70988.2026.00043（✅arXiv+✅Crossref，IEEE AITest 2026，pp. 235-248） | LLM Agent+编程 Agent 混合测 RESTful API，OpenAPI/Swagger 输入，人工审校 LLM 产物，单元+系统两级覆盖（GPT-4o/DeepSeek 实测单元 94%/98%、系统 79%/78%）。**venue 注意**：IEEE AITest 是 AI 测试专门会，非 ICSE/FSE/ASE 核心，引用时如实标注。L4 人机协同确认门设计佐证 |
| 37 | Keploy | github.com/keploy/keploy（✅GitHub API：18892★，当天有 push，Go/Apache-2.0） | 流量录制回放→自动生成测试+Mock。**纠错（重要）**：旧口径"Windows 需 WSL2"已过时——现行官方文档明确 x86-64 Windows **原生运行（no WSL, no Docker, no Administrator）**，仅 Windows on ARM 必须 WSL。v1 仍不采用（对微型语料过重），但展望章 Windows 限制表述以此为准 |
| 38 | Pynguin（Lukasczyk/Kroiß/Fraser，帕绍大学 se2p） | 原始论文 SSBSE 2020：DOI 10.1007/978-3-030-59762-7_2（arXiv:2007.14049）；实证长文 EMSE 2023：DOI 10.1007/s10664-022-10248-w；工具演示 ICSE 2022 Companion（arXiv:2202.05218）；GitHub se2p/pynguin 1395★ 活跃（✅Crossref+✅GitHub） | SBST Python 基线（谱系旧底稿已核验，本轮补齐精确 DOI 与活跃度）。相关工作对比叙事：搜索式生成 vs 本项目 LLM 生成+覆盖率反馈闭环 |
| 39 | EvoSuite（Fraser & Arcuri） | 工具论文 ESEC/FSE 2011：DOI 10.1145/2025113.2025179；whole-suite 算法长文 IEEE TSE 39(2) 2013：DOI 10.1109/TSE.2012.14（✅Crossref；无 arXiv，2011 会议论文未上 arXiv 勿编造）；GitHub EvoSuite/evosuite 920★ 低维护（2025-02 最后 push） | Java SBST 事实基准；仅相关工作引用（本项目被测对象是 Python，无工程落点） |
| 40 | ChatUniTest（Chen 等，浙江大学 ZJU-ACES-ISE） | arXiv:2305.04764 + DOI 10.1145/3663529.3663801（FSE 2024 Demonstrations Track，✅arXiv+✅Crossref）；GitHub ZJU-ACES-ISE/ChatUniTest 175★（生态仓 chatunitest-core/-maven-plugin 活跃至 2026-03） | **纠错**：记忆编号 2404.03219 实为 3D 分割论文（iSeg，SIGGRAPH Asia 2024）；记忆仓库 ChiChengHub/ChatUniTest 不存在（404）。生成-验证-修复三阶段+自适应聚焦上下文；注意是 Demo track 短文非主会长文。R-Loop 同路线学术佐证 |
| 41 | Agent Q（Putta 等，MultiOn+Stanford） | arXiv:2408.07199（✅arXiv，**无官方开源**） | **不采纳（错位引用）**：它是 MCTS+自我批评+off-policy DPO 训练 web 决策 agent 的研究，"固化"的是**模型权重**不是可执行测试脚本——"探索一次→固化为传统脚本"的正当谱系是 record/replay 工具（API 侧=Keploy，UI 侧=Playwright codegen）。若表述受其启发，只能写"agent 探索式轨迹收集"最抽象一层 |
| 42 | Langfuse | github.com/langfuse/langfuse（✅GitHub API：36700★，当天有 push，YC） | 定位=LLM tracing/observability/evals 平台，**非软件测试工具**——其 evals 是 LLM 输出质量评估。展望素材：Agent 测试过程可追踪可审计（本项目以 SQLite 审计台账+skill_invocations+llm_calls 自研实现同等能力） |

**§8 纠错小结**（延续"凭记忆写编号必错"教训，累计第 4 次实锤）：ChatUniTest 编号 2404.03219→**2305.04764**、组织 ChiChengHub→**ZJU-ACES-ISE**；Keploy"Windows 需 WSL2"为过时口径（x86-64 已原生）；Agent Q 挂"UI 脚本固化"属错位引用；AutoRestTest 主会论文标题不含该词（工具名 vs 论文标题陷阱）。
