# 《Agent Loop 同类方案调研——开源项目与论文》

> 服务对象：`docs/设计方案-AgentLoop文件级实施设计-20261120.md`（五包设计）
> 调研目的（用户定调）：**避免重复造轮子 + 避免翻车**——找可直接借鉴的想法与机制，找别人踩过、我们必须绕开的坑，映射回五包设计逐项核对。
> 版本：v1.0　日期：2026-11-20　方法：Web 检索 + GitHub API 元数据核验（核验时间同日）
> 与宏观稿 §4 的分工：宏观稿已考察 Playwright Test Agents / MCP / Agent Skills / OpenHands SDK / Voyager 的**架构级**取舍，本文不重复展开这五者的架构面，只补安全与实现层细节；本文主体是宏观稿**没有覆盖**的十一个新对象。

---

## 0. 检索与核验方法

- 检索线四条：A 测试生成专项 / B Agent 循环与接口 / C 自进化与经验 / D 安全与供应链。
- GitHub API 核验（星标/许可/最后 push），直连可用（无需代理）；核验结果见 §1 总表。**未核验的两类如实标注**：Cover-Agent（原 Codium-ai 仓库 404，疑似改名/归档——以论文与官方文档为真源）、ExpeL/openai-codex（API 配额耗尽未核验——以 arXiv/OpenAI 官方文章为真源）。
- 纳入标准：与"LLM Agent 测试生成/工具循环/插件安全"直接相关、有可检验的论文或开源实物；排除纯营销页。

## 1. 候选总表（核验 2026-11-20）

| 对象 | 类型 | 核验 | 关键事实 | 对我们的角色 |
|---|---|---|---|---|
| **SWE-agent**（arXiv:2405.15793） | 论文+仓库 | 20,838★ MIT，活跃 | ACI 三原则：动作简化/易用/高质量反馈；编辑即 lint 拦截；窗口化文件视图；搜索结果截断 | B 线核心借点 |
| **OpenHands**（原 All-Hands-AI） | 仓库 | 93,899★ MIT，当日活跃 | Stuck Detector（重复动作/重复错误检测）+ `execute_bash` 超时参数一等公民 | B 线核心借点 |
| **Agentless**（arXiv:2407.01489） | 论文+仓库 | 2,138★ MIT，2024-12 后停更 | 固定三段流水线（定位/修复/验证）打平/超过自主 Agent，更便宜可控 | **警示牌**（2×2 的对照组论点） |
| **AutoCodeRover**（arXiv:2404.05427） | 论文+仓库 | 3,104★，自定义许可 | 结构感知 AST 检索 API（搜类/搜方法）+ SBFL | 只借想法（许可不抄码） |
| **TestGen-LLM**（arXiv:2402.09171，Meta） | 论文（无公开码） | arXiv 属实 | 过滤级联：可构建→稳定通过（排 flaky）→覆盖增量（排重复）→断言健全；产线部署 | A 线核心借点 |
| **CodaMosa**（ICSE 2023） | 论文+产物 | 论文属实（~723 引） | SBST 覆盖平台期 → LLM 种子逃逸局部最优 | A 线（L2 急救的先例） |
| **CoverUp**（arXiv:2403.16218） | 论文 | arXiv 属实 | 覆盖引导迭代对话生成；**tautological 断言坑与对策** | A 线（oracle 自证坑） |
| **SWT-bench**（arXiv:2406.12952，NeurIPS 2024） | 论文+仓库 | 93★ MIT，活跃 | fail-to-pass 测试作修复验证器，过滤效力翻倍 | A 线（verify 冻结口径） |
| **失败复现测试分类学**（arXiv:2611.13395，2026-11） | 论文 | arXiv 属实 | 对 OpenHands/AssertFlip 生成测试失败原因做分类学 | **diagnose 节点分类对齐对象** |
| **Voyager / ExpeL / AWM**（2305.16291 / 2308.10144 / AWM） | 论文 | Voyager 7,337★ MIT；ExpeL 仓库未核验 | 技能库+自验证；NL 洞见提取；结构化工作流归纳 | C 线（阶段 3 前例） |
| **MCP 工具投毒/rug pull**（Invariant Labs 2025-04） | 安全披露 | 原文属实 | 工具描述藏指令；`list_changed` 批准后偷换定义；GitHub MCP 真实漏洞链 | D 线核心坑 |
| **Codex CLI 沙箱档**（OpenAI 官方） | 文档 | 官方文章属实 | read-only / workspace-write（默认禁网）/ danger-full-access × 审批策略两轴 | D 线（权限模型同构） |
| **AutoGPT 失败案例集**（vectara/awesome-agent-failures） | 仓库 | 223★ Apache-2.0 | 无限循环/重复动作/上下文丢失的实证案例 | B 线警示牌 |
| Pynguin / EvoSuite | 仓库 | 1,395★ MIT / 922★ LGPL-3.0 | SBST 基础设施 | 背景板（A 轨已调研过，不重复） |

---

## 2. A 线：测试生成专项

### 2.1 TestGen-LLM（Meta，arXiv:2402.09171）——过滤级联是主答案

**做法**：不做从零生成，做**存量测试改进**；每个候选测试必须闯四关级联：①可构建（编译/收集）→ ②稳定通过（重复运行排 flaky）→ ③覆盖增量（不与存量重复）→ ④断言健全（与既有断言不雷同）。产线漏斗：75% 可构建 → 57% 稳定通过 → 25% 有覆盖增量；57% 的测试类被改善。被接受的测试 73% 落地、75% 后来被工程师继续编辑——**机器产物作为人类工作起点**的定位被验证。

**对我们的映射**：
- 我们的闸其实已同构（collect 闸≈①、FLAKY_RERUNS≈②、per_file 缺口对账≈③、expected_basis 纪律≈④），但**分散在各节点**。TestGen-LLM 验证了"级联顺序=便宜在前"的正确性——我们的实现恰好是这个顺序，被外部验证。
- **借点 1**：把它的漏斗数字（生成 N → 可构建 → 稳定通过 → 覆盖增量）作为我们论文的过程指标模板——比单一"检出率"更能展示工程质量，且全部可从 `test_case_results`+`llm_calls` 算出。
- **借点 2**："57% 稳定通过"意味着 **flaky 是高频形态**（近半生成测试不稳）——我们 FLAKY_RERUNS 默认关，实验臂应至少开一档对照（MVP 冒烟格建议开 3）。

### 2.2 CoverUp（arXiv:2403.16218）——tautological 断言坑（重要新坑）

**坑**：LLM 为过闸会生成**自证式断言**——`assert f(x) == f(x)`、或先跑一遍被测函数拿输出当期望值（oracle 来自被测对象自身=永真）。CoverUp 的对策：对生成测试做**行为验证**——与既有代码行为比对，丢弃无意义断言。

**对我们的映射**：369 家族"自导自演"闸（JS 文本启发式+`_mutates_sut_state`）已覆盖"改状态再断言"形态，但**同型表达式永真断言**（AST 级 `assert expr == expr`）没有确定性检测。**设计修正 1**（进包 1）：静态闸补确定性 tautology 检测——AST 比较两侧同构（含函数调用与自身比较）即剔除，~30 行，挂进现有三道闸管线。

### 2.3 SWT-bench + 失败复现分类学（arXiv:2406.12952 / 2611.13395）

**做法**：把"复现 bug 的测试"本身作为交付物（fail-to-pass 口径），用测试过滤错误修复，效力翻倍。**2026-11 的后续论文**对生成测试的失败原因做了**分类学**——大量失败不是"测试逻辑错"而是**环境搭建错/测试装置错**（框架配置、fixture、前置状态）。

**对我们的映射**：
- fail-to-pass 口径我们 A 轨已采用（BugsInPy 协议）——被主流 benchmark 验证。
- **分类学论文直接支持 diagnose 节点的 kind 分类**：我们的四分类（脚手架/环境/产品缺陷/预期争议）与其"环境搭建/测试装置/语义错误"高度同构。**借点 3**：diagnose 提示词的 few-shot 示例按该分类学措辞组织，并把它的分类作为我们 kind 命名的外部锚点（论文可引）。
- 它也验证了 0-A（验证资产快照）：pass-to-pass 稳定性是测试作为验证器的前提，用例集漂移会直接毁掉验证效力。

### 2.4 CodaMosa（ICSE 2023）——L2 急救的先例

SBST 到达覆盖平台期时注入 LLM 种子逃逸局部最优。我们 L2 覆盖率急救（零覆盖→扩圈→回退全库）是同族思想（搜索卡住→LLM/规则介入）。**验证**：平台期检测作为触发条件是正确设计（我们的触发=锁定模块零覆盖）。无新增设计修正。

---

## 3. B 线：Agent 循环与接口

### 3.1 SWE-agent ACI（arXiv:2405.15793）——接口设计与模型同等重要

**做法**：不给模型裸 shell，给**受限动作集**+三条接口原则：任务相关动作简化、易用、高质量反馈。具体护栏：编辑命令发出即 lint，语法错**当场拒绝**；文件查看器窗口化（只显示当前位附近行）；搜索结果**截断封顶**防上下文淹没。

**对我们的映射**：
- 12 项 capability 小动作空间 = ACI 第一原则的直接实践，被验证。
- AST edit 分类闸（包 1 §3.6）= "编辑即 lint 拦截"的同构物，被验证。
- **借点 4**（设计修正 2，进包 1）：`test.observe.read` 的输出必须**截窗封顶**（单文件 ≤200 行窗口+grep 结果 ≤50 条）——我们只控了观察包 token 预算，没控单次能力输出的行数爆炸（一个大文件能把 1.5k tok 预算全吃掉还截断在半截）。

### 3.2 OpenHands Stuck Detector + 超时实践

**做法**：Stuck Detector 在事件流上每步检测**重复动作/重复错误**；`execute_bash` 把 timeout 作为工具参数一等公民；明确语义"终端超时=失败回合，须先观察状态再重试"。

**对我们的映射**：
- 重复动作检测 = 我们的动作指纹闸，被验证；**补充坑**：他们把"连续错误"（不只是重复动作）也算 stuck。**设计修正 3**（进包 1）：tool_loop 加 denied 连击计数——连续 ≥3 次不同动作全被 policy 拒绝 = 策略与权限面失配，stop("policy_deadlock")，避免模型在权限墙外换着花样撞（每次 denied 都烧 token）。
- 超时一等公民支持 **D7 拍板=全链路包裹**：OpenHands 把超时做进工具接口本身而非外层等待，与我们 coreutils `timeout` 包裹 `exec_in` 的方向一致。

### 3.3 Agentless（arXiv:2407.01489）——最重要的警示牌

**事实**：固定三段流水线（定位→修复→验证），LLM **从不决定下一步动作**，在 SWE-bench Lite 上打平/超过当时的自主 Agent，成本更低、可解释、可控（"Agentless 悖论"）。

**对我们的映射**：这不是否定 Agent loop，是**实验设计的军令状**——我们的 2×2 预注册判据"D 不优于 C → 回退或收窄自主面"（设计稿 §6.5）正是应对这个悖论的正确姿势，外部证据要求我们必须认真对待"B/D 臂可能不赢"的结果并如实报告。**另借点 5**：Agentless 的"定位先行"层级（先粗后细的定位消耗大部分预算）验证了我们 strategy 节点把预算倾斜给定位/观察类能力（R0）而非生成（R1）的优先序。

### 3.4 AutoCodeRover（arXiv:2404.05427）

结构感知检索（AST 上搜类/方法签名）优于裸文本 grep。**验证**了我们 project_index + scope 锁定的路线；**借点 6**（可选，阶段 1.5）：`test.observe.read` 加 `search_class/search_method` 结构化检索模式（索引里有 symbols 清单，零新增索引成本），模型"找代码"从 grep 升级为结构查询——ACI 的"高质量反馈"同款。许可为自定义 NOASSERTION——**只借想法不抄代码**。

### 3.5 AutoGPT 失败案例集（vectara/awesome-agent-failures）

无限循环/重复同一动作/无界重试/上下文丢失的实证集。主流缓解=动作指纹、强制停止条件、有界重试、进度追踪——我们五条停止条件+预算三上限已全覆盖；无新增修正，作为论文"为什么要有这些闸"的引用素材。

---

## 4. C 线：自进化与经验（阶段 3 前例）

| 方案 | 机制 | 我们对应物 | 借/避 |
|---|---|---|---|
| Voyager | 技能库+迭代提示+**LLM 自验证**入库 | 阶段 3 技能库 | **避**：LLM 自评即正确（宏观稿已列）；**借**：技能按适用条件检索复用 |
| ExpeL | 从自身轨迹提取**自然语言洞见**（成功+失败） | trapkb（失败模式 NL 条目） | 借：成功轨迹也是经验源（trapkb 目前只有失败侧）；无 fine-tuning 纯提示注入=我们的路线 |
| AWM | 归纳**结构化多步工作流**（子程序）复用 | 种子模板（结构样板）+路线编排 | 借：workflow 版本绑定任务类型；**避**：归纳过度拟合已见任务（我们已有 held-out 验证对策） |

**借点 7**（进阶段 3 设计，暂不动包 1/2）：trapkb 增设"成功侧"来源——从 fixed_verified 的验证任务轨迹提取"这次适配为什么成功"的 NL 条目，与失败条目同表同纪律（来源/证据/适用范围）。ExpeL 证明双侧经验都有效。

---

## 5. D 线：安全与供应链（包 2 的外部依据）

### 5.1 MCP 工具投毒与 rug pull（Invariant Labs 2025-04/05）

**坑**：①工具描述里藏隐形指令（LLM 读得到、人看不到）；②服务端在用户批准后经 `list_changed` **偷换工具定义**（rug pull）——信任一次的模型被后续变更背叛；③真实漏洞链：GitHub MCP（14k★量级集成）被组合利用访问私有仓库。

**对我们的映射**：
- 我们的"目录 digest 固定+版本固定"正是 rug pull 的**结构性防御**：内容一变摘要就变=新条目=新审批——这条论证直接进论文（比"安全最佳实践"更有攻击-防御对应性）。
- **新发现的同类微型坑（设计修正 4，进包 0-B）**：`registry.register()` 的 upsert 会在**版本号不变**时静默覆盖 `input_schema`——这就是一个 mini rug pull（注册时批的是 A schema，后续 upsert 换成 B）。修法：upsert 时若 `input_schema` 与库中不同而 `version` 未变，写审计告警行（拒绝或告警二选一，建议先告警——存量 skill 会踩）。
- "工具描述是不可信输入"已是我们纪律（宏观稿 7.3 第 6 条），Invariant 的 PoC 给了可引的具体案例。

### 5.2 Codex CLI 沙箱档位（OpenAI 官方）

两轴设计：**沙箱模式**（read-only / workspace-write / danger-full-access）×**审批策略**（ask / on-request / never）正交组合；workspace-write **默认禁网**；官方自己警告 danger-full-access+never 仅限明确知道自己在干什么的人。

**对我们的映射**：
- R0-R4 风险级 × 任务模式矩阵 = 同构的两轴设计，被工业实践验证。
- "workspace-write 默认禁网"= 我们"任务容器 network=none+平台侧预拉取"的同款决策，可引为惯例依据。
- **借点 8**（进包 2 文档措辞）：论文里把我们的权限模型表述为"沙箱能力与审批策略正交"，与 Codex 对照=显出设计成熟度。

---

## 6. 逐包影响汇总表（本调研对五包设计的净作用）

| 包 | 被验证的设计（外部证据） | 新增设计修正 | 出处 |
|---|---|---|---|
| 包 0 | 0-A 快照（pass-to-pass 稳定性前提）；0-C exec 层超时（OpenHands 一等公民实践→**D7 建议=全链路包裹**） | 修正 4：register() schema 静默变更=mini rug pull → 审计告警 | SWT-bench / OpenHands / Invariant |
| 包 1 | 小动作空间+编辑即 lint（ACI）；重复动作闸（Stuck Detector/AutoGPT）；fail-closed 保留证据（超时=失败回合先观察）；四分类 diagnose（分类学论文） | 修正 1：tautology 断言确定性检测；修正 2：read 输出截窗封顶；修正 3：denied 连击 ≥3 → policy_deadlock 停 | SWE-agent / OpenHands / CoverUp / arXiv:2611.13395 |
| 包 2 | digest 固定=rug pull 结构性防御；两轴权限模型；默认禁网惯例 | 措辞强化：沙箱能力×审批策略正交（对齐 Codex 表述）；无代码变化 | Invariant / Codex CLI |
| 包 3 | — | denied/stuck 计数进 eval_metrics（Stuck Detector 同款指标形态） | OpenHands |
| 包 4 | 预算倾斜定位类（Agentless 定位先行） | 修正 5：过程指标采用 TestGen-LLM 漏斗形态（生成→可构建→稳定过→覆盖增量）；FLAKY_RERUNS 实验臂至少开一档 | Agentless / TestGen-LLM |
| 阶段 3 | held-out+人工推广优于 LLM 自验证 | 借点 7：trapkb 增成功侧来源（ExpeL 双侧经验） | Voyager/ExpeL/AWM |

**净结论**：五包设计无一被外部证据推翻；新增 5 处修正（1 tautology 闸 / 2 截窗 / 3 policy_deadlock 停 / 4 register 审计 / 5 漏斗指标）+2 处文档强化，全部是小增量，不动架构。最大的外部约束是 Agentless 悖论——它不改变设计，但把 2×2 的诚实判据从"应该有"升级为"必须有"。

## 7. 明确不学清单（避免翻车）

1. **Voyager 的 LLM 自评即验证**——技能入库必须确定性证据（held-out+人工）。
2. **Playwright healer 的"功能坏了就跳过测试"**（宏观稿已列，重申）——真实失败必须是缺陷证人。
3. **AutoGPT 的无界循环与自主提预算**——预算三上限+五停止条件是底线。
4. **MCP 的"连上即信任/描述即事实"**——目录+摘要+审批，缺一不可。
5. **Codex 的 danger-full-access+never**——我们的 R4 永远属于管理员。
6. **Agentless 的"因此不要 Agent"过度推论**——它的正确教训是"自主性必须用实验证明增量"，不是"自主性无用"；我们做 2×2 而不是砍功能。

## 8. 诚实边界

- Cover-Agent / ExpeL / openai-codex 仓库未能核验（改名/迁移/API 配额）——以论文与官方文档为真源，引用时标注；不影响任何设计修正的有效性（修正 1-5 的依据均来自已核验对象）。
- AutoCodeRover 自定义许可——只借结构检索思想，零代码复制；Agentless/MIT 与 SWE-agent/MIT 若未来引代码需保留许可声明。
- arXiv:2611.13395 为 2026-11 新文（OpenReview 在审），作为分类学**对齐参考**引用，不作为结论依赖。
- 本调研未发现任何"测试 Agent 自主接入插件"的完整开源先例（最接近的是 MCP 生态的受控接入与 Voyager 的技能库）——**我们设计的组合（测试域+目录制+任务沙箱+审计）在检索范围内无直接同款**，这既是创新点也是风险（无前人验证=实验更要做扎实）。

## 9. 检索来源

- [TestGen-LLM (arXiv:2402.09171)](https://arxiv.org/html/2402.09171v1) / [Engineer's Codex 解读](https://read.engineerscodex.com/p/metas-new-llm-based-test-generator)
- [CodaMosa (ICSE 2023 PDF)](https://www.carolemieux.com/codamosa_icse23.pdf) / [MSR 页](https://www.microsoft.com/en-us/research/publication/codamosa-escaping-coverage-plateaus-in-test-generation-with-pre-trained-large-language-models)
- [SWT-bench (arXiv:2406.12952)](https://arxiv.org/html/2406.12952v3) / [官网](https://swtbench.com) / [GitHub](https://github.com/logic-star-ai/swt-bench)
- [失败复现测试分类学 (arXiv:2611.13395)](https://arxiv.org/html/2611.13395v1) / [OpenReview](https://openreview.net/forum?id=OLHKkIidVs)
- [Agentless (arXiv:2407.01489)](https://arxiv.org/abs/2407.01489) / [GitHub](https://github.com/openautocoder/agentless)
- [SWE-agent (arXiv:2405.15793)](https://arxiv.org/html/2405.15793v3) / [ACI 文档](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md)
- [AutoCodeRover (arXiv:2404.05427)](https://arxiv.org/html/2404.05427v1) / [GitHub](https://github.com/AutoCodeRoverSG/auto-code-rover)
- [Voyager (arXiv:2305.16291)](https://arxiv.org/abs/2305.16291) / [GitHub](https://github.com/MineDojo/Voyager)
- [ExpeL (arXiv:2308.10144)](https://arxiv.org/abs/2308.10144)
- [CoverUp (arXiv:2403.16218)](https://arxiv.org/abs/2403.16218)
- [OpenHands Stuck Detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector) / [OpenHands GitHub](https://github.com/OpenHands/OpenHands)
- [MCP Tool Poisoning (Invariant Labs)](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) / [CSA 研究注](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-ai-agent-exfiltration-2) / [Lens 分析](https://lenshq.io/blog/mcp-security-tool-poisoning-threat-model)
- [Inside the Codex sandbox (OpenAI)](https://openai.com/index/inside-the-codex-sandbox)
- [AutoGPT 失败案例 (vectara/awesome-agent-failures)](https://github.com/vectara/awesome-agent-failures/blob/main/docs/case-studies/autogpt-planning-failures.md)
