# 目标流程愿景 与 现状差距映射（2026-10-26 与用户对齐稿）

> 用途：本文是愿景的权威记录。AGENTS.md「当前状态」只留摘要与指向。
> 结论先行：愿景整体可行，主链路约 60% 已有落点；真正要新做的是 5 件事（见 §4 落地顺序）。

## 1. 用户愿景流程（整理版）

1. 应用层输入：项目地址（可克隆）+ 自然语言需求。
2. LLM 受测试 prompt 约束做 plan 与 execute；按固定格式输出测试脚本设计。
3. 用户可在 AI 应用层插入 / 用 AI 编写额外用例，插入测试脚本（额外用例也进 plan 与执行）。
4. 执行前准备：
   - Tool① 语言占比检测，返回数据；占比过低的语言不拉镜像（做静态/表面测试）。
   - Tool② 按条件调用对应预构建 Docker 镜像（提前备好以省时间）。
   - Tool③ 固定格式 tool 让 Agent 按需求生成用例，并对用例**二次审查**。
   - Tool④ 执行测试；失败→检查用例→回炉重造→retest，直到成功（设 3 轮上限防重复）。
5. 测试结束：所有测试数据与需求对比给评价；工具集 = 测试报告 / 测试分析 / 数据分析 / 需求对比图生成等（待协同设计 → 见 §3）。
6. 对测试报告数据做合理性分析（报告的二次审查），不合理 → 用例回炉重测。
7. 总纲：流程必须符合 测试分析 → 设计 → 执行 → 评估 四阶段（教科书口径见 docs/testing-theory.md）。

## 2. 愿景 ↔ 现状映射

| 愿景条目 | 现状落点 | 状态 |
|---|---|---|
| 输入 repo + 需求 | cli.py `new`：projects + test_tasks(requirement_text) | ✅ 已实现 |
| 语言占比检测 tool | graph/nodes.py `analyze`：后缀统计→占比→主语言→分级 | ✅（口径差异见 §2.1） |
| <10% 不拉镜像 | **口径 b 已实施（2026-10-28 拍板）**：主语言不在 python/java → 整仓 static_only；模块级拆分（口径 a）列论文未来工作 | ✅ 口径已对齐 |
| 预构建镜像调用 | docker/ 两 Dockerfile + config.IMAGE_PY/JAVA + ensure_container 幂等复用 | ✅ 已实现 |
| LLM 生成用例（固定格式） | skills/gen_tests.py：五方法提示词 → JSON → 清洗 → 写工作区 → 回灌容器 → 落库 | ✅ 已实现 |
| 用例**二次审查** | 静态闸（AST import 符号校验）+ 可收集闸（容器内 --collect-only）两道已生效；LLM 交叉语义审 v1 未做（§3.6 第 3 道闸，列未来工作） | ⚠️ 两道闸已生效，LLM 闸未做 |
| 容器执行测试 | sandbox/runner.py + run_tests skill（高风险、确认门 fail-closed） | ✅ 已实现 |
| 失败→回炉→retest，3 轮上限 | graph/repair.py：归因三分类 + 指纹熔断(≥2) + 轮次预算(3) | ✅ 已实现 |
| 归因分流（env/case/product） | repair.classify_failure + decide 分流 repair_env / repair_gen | ✅ 已实现 |
| 评估阶段工具集（§1.5） | skills/evaluate.py 五工具（collect_metrics/map_requirements/gen_report/review_report/gen_charts）+ review 节点编排（E5，2026-10-28） | ✅ 已实现 |
| 报告合理性二次审查（§1.6） | review_report 双闸（规则闸纯函数先行 + LLM 闸收窄聚焦）落 test_reports.review；**v1 收口：reject 一律不回炉、如实标记 rejected 放行**（run46 实锤 reject 回炉×快照漂移=14 轮死循环；受控重写回炉列下一轮，见 §3.4） | ✅ 已实现（语义 v1 收口） |
| 人工插入/编辑用例 | L1 已建：PUT /api/scripts 落库 source='imported' + 重建工作区 test_imported.py 随快照进容器 | ✅ 已实现 |
| 测试分析阶段的条件/RTM/可追溯性 | plan 节点 + test_conditions 表（第 14 张）+ rtm_maps 矩阵快照（第 15 张，map_requirements 每次评估覆盖写） | ✅ 已实现 |
| 用例六要素结构化（编号/标题/优先级/输入/步骤/预期） | test_cases 六要素列 + expected_basis 证据链落库（阶段 3 be89a62，实测 LLM 输出"需求明示"） | ✅ 已实现 |

### 2.1 10% 阈值口径（需拍板）

两条路：**(a)** 实现真正的多模块拆分——每语言按目录聚类、占比 <10% 的模块标 static_only，主语言 full（对应 schema 的 modules 表设计，工作量 2~3 天）；**(b)** 论文口径改为"主语言不在深度支持集 → 整仓 static_only"，v1 不拆模块。建议 (b) 先行、(a) 作为论文"未来工作"，因为单模块已能跑通全部实验，拆分只增加演示复杂度。

## 3. 协同设计：评估阶段工具集 + 两处升级

原则：评估的"数字"全部由确定性代码从 SQL 算出，LLM 只负责"叙述与判断"——报告可信的地基。所有工具走 SkillRegistry（自动留 skill_invocations 审计，论文"工具调用成功率"指标白拿）。

### 3.1 collect_metrics —— 测试数据汇总 tool（确定性，risk=low）
输入 task_id；查 test_reports / test_cases / skill_invocations / container_jobs / feedback_annotations；
输出结构化指标包 JSON：通过率、行覆盖率、缺陷按归因分布、R-Loop 每轮轨迹（指纹/verdict/原因）、工具调用成功率、各阶段耗时。**LLM 禁止自己算数。**

> **快照一致性补记（run46 实锤 + 根修，2026-10-28）**：collect_metrics 的 tool_invocations 统计包含"评估工具自身"的调用台账，任何两次 collect 之间数字必然增长。因此评估链确立**单快照语义**：review 节点 collect 一次，同一份 metrics 快照经 args 下传给 gen_report / review_report / gen_charts——叙述、审查、图表三类产物全部派生自同一时点，彼此零漂移；与"之后重跑 collect 得到的数字"不同属台账自然增长的诚实语义（LLM 审查提示词已声明不对台账字段判不一致）。根修落点：evaluate.py `_charts` 与 `_report`/`_review` 同款 args 下传。

### 3.2 plan 节点 + test_conditions 表 —— 补"测试分析阶段"（最大缺口）
analyze 之后、provision 之前加一个 `plan` 节点：LLM 把 requirement_text 拆成结构化测试条件（requirement_ref / condition / priority / method_hint），落新表 `test_conditions`（schema 13→14 张）。
这同时解决：①需求可追溯（防变更影响分析：条件→用例→结果 反查）；②"LLM 做 plan"的落点——**plan 是产物，不是流程控制**；③§3.3 需求对比的数据源。

### 3.3 gen_tests 升级：六要素结构化
提示词输出从 `{name, method, code}` 扩为 `{name, method, priority, input, steps, expected, code}`，test_cases 加对应列。理论章"用例规格六要素一个不缺"从此有数据表支撑；expected 字段显式化"断言先于执行"。同时 test_cases 加 script 关联已有，人工插入的用例 source='imported' 走同一表。

### 3.4 review_report —— 报告合理性二次审查 tool（§1.6 的落点，双闸）
- **规则闸（确定性，先行）**：exit=0 但 failed>0；coverage>100% 或 None；报告数字 vs 指标包逐个比对；耗时离群；归因为 product_defect 却写成"已解决"。
- **LLM 闸**：报告叙述与原始数据是否一致、结论是否有证据。
- 输出 pass / reject(reasons)，结论落 test_reports.review。**v1 收口（run46 实锤，2026-10-28）**：reject 一律**不回炉**——如实标记 review_status=rejected 放行（原因留痕），因为"reject → repair_gen 回炉"与叙述快照时点漂移叠加会造成 14 轮死循环；"reject → 受控重写报告一次"列为下一轮工作（叙述快照时点已根修：评估链单快照下传，见 §3.1 补记）。stop（product_defect 证人冻结）是上报不可被审查改判。

### 3.5 map_requirements + gen_report + gen_charts
- `map_requirements`（确定性+LLM 辅助）：test_conditions ↔ test_cases ↔ 执行结果 → RTM 矩阵（覆盖/缺口）。
- `gen_report`（LLM）：模板固定（结论/四阶段回溯/缺陷清单/风险建议），prompt 写死"不得出现输入数据之外的数字"。
- `gen_charts`（matplotlib）：需求覆盖图、覆盖率随轮次趋势、失败归因饼图、阶段耗时瀑布——论文插图直接产出。

### 3.6 生成用例的语义二次审查（§1.4-③ 的落点，三层闸门，规则先行）
1. **静态闸**（AST）：import 是否可解析、调用的函数/签名在被测源码中是否存在——确定性代码，零成本。
2. **可收集闸**：容器内 `pytest --collect-only`，收集失败即废案。
3. **LLM 交叉审**：以审查者角色审断言是否对应需求（≠自审走过场）。不过审 → 单独重造该用例，不拖累整批。

## 4. 落地顺序建议

1. repair.py 纯函数单测（半天，答辩证据链）
2. plan 节点 + test_conditions 表（1~2 天）
3. gen_tests 六要素 + 静态/可收集审查（2 天）
4. 评估工具集：collect_metrics → gen_report → review_report → map_requirements → gen_charts（3~4 天）
5. L1 界面（人工插用例 + 确认 + 报告展示；CLI 已能跑通全部逻辑，界面最后做）

## 5. 已拍板的纠偏口径（答辩表述）

1. **不承诺"直到成功"**：product_defect 是被测程序真有缺陷，重造用例去迁就它 = 系统造假。正确口径是"达标或预算耗尽时如实上报"，三停（准出/熔断/预算）是亮点不是缺陷。
2. **"回炉重造"只适用 case_defect**：env 失败修环境（repair_env 已实现），product_defect 报缺陷，不进用例回炉。
3. **固定图做骨架，LLM 的 plan 是节点产物**：主干编排交给 LLM 自由发挥 = 不可复现、不可画图、答辩难讲。plan 落在 plan 节点与 gen_tests 两处，"LLM 受 prompt 限制做 plan"依然成立。
4. **镜像预构建可行且已实现**（分析见 §6）。

## 6. 预构建镜像可行性分析（用户点名）

**结论：可行、方向正确、且当前代码已经在这么做。** 论据：

1. **时间账**：现场 build 一次（装 pytest/cov/ruff/bandit 或 Maven 工具链）分钟级；预构建后 `docker run` 秒级。且 SandboxRunner 常驻容器 + docker exec 驱动，R-Loop 多轮补测不重启容器，边际成本毫秒级。
2. **可复现账**：工具链版本锁死在镜像里 → 实验数据可复现，这是论文实验的硬要求，预构建是唯一稳妥解。
3. **安全账**：镜像薄（只工具链），被测项目依赖运行时装、隔离在容器内；非 root tester + --cpus/--memory 限额（config.DEFAULT_*）。
4. **要补的三件小事**：
   - 镜像构建脚本化：`scripts/build-images.sh` 收拢两个 docker build 命令（现在只在 Dockerfile 注释里），论文附构建清单。
   - 依赖缓存的诚实口径：首次 `pip install -r requirements.txt` / `mvn dependency:go-offline` 仍需网络耗时——可用 docker volume 缓存（Java 的 ~/.m2 收益最大），论文里如实写"工具链预构建、依赖按项目装"的分层。
   - **实验送分**：container_jobs 有 started_at/finished_at，可做"预构建镜像 vs 现场 build"耗时对比实验——预构建的价值直接变成论文里的一组数据。
5. **局限（诚实边界）**：语言矩阵扩展（go/node/...）要维护镜像集，建议 config 里做成镜像注册表 dict；项目私有依赖（内网源、系统库）预构建镜像管不了，依赖容器内安装兜底。

## 7. 归因问题设计定稿（2026-10-26 第二轮对齐）

用户问题："如果失败是产品真有 bug，重造用例去迁就它等于系统造假——但测试中怎么知道是用例错了还是产品有 bug？"

**定论：自动归因不可能 100% 准——这是测试预言（oracle）问题的本质：预期值必须有独立于被测实现的来源，否则永远无法裁决"用例 vs 产品"。工程正解 = 证据分层打分 + 置信度 + 不确定时的保守默认：宁可误报，不可造假。** 把产品 bug 当用例错去改断言 = 系统造假（不可接受）；把用例错当"疑似产品缺陷"上报 = 误报（人工过滤即可，方向诚实）。

### 7.1 五个证据源（按强度排序，均可落地）

1. **预期值来源 `expected_basis`（最强证据）**：gen_tests 输出加字段，每个断言标注预期值出处：`需求明示` / `文档(docstring)` / `源码推导` / `无依据`。判读规则：失败 + 需求明示 → 强 product_defect 信号；失败 + 源码推导 → 循环断言（用预期=从代码推的值去测代码，永远测不出 bug），case_defect 信号。**铁律：预期值来自需求明示/docstring 的失败用例永不删除、永不为它改断言。** 这把"断言先于执行"的 prompt 纪律升级成了可审计的数据。
2. **traceback 位置（零成本）**：异常最深帧在产品源码文件 → product 信号；在测试文件里、或导入期（ImportError/SyntaxError/fixture 错）→ case 信号。
3. **同点互证**：同一函数的其他用例通过 → 怀疑该用例；多个独立用例在同一行为点失败 → 怀疑产品。需 pytest 加 `-v` 拿通过用例名单（当前 -rfE 只给失败名单）。
4. **LLM 独立复审（只对模糊失败花这一次钱）**：审查者角色、不给看原断言，独立推导预期值再比对——与断言一致 → product 证据；不一致 → case 证据。
5. **变异标定（论文实验，落实④）**：向 demo 项目注入已知缺陷（mutant）+ 预置已知坏用例（翻转预期值/错 import），测归因器的混淆矩阵 → 论文"归因准确率"有了可控真值的数据源。变异测试由此获得系统落点：归因标定仪器 + 有效用例率指标（同时解决理论章五法表与 prompt_library 第五法不一致的问题）。

### 7.2 R-Loop 分流策略（升级后；✅ 2026-10-27 已实现，见 commit be89a62）

| 归因（含置信度） | 动作 |
|---|---|
| env | repair_env（现状保留） |
| case_defect 高置信 | 按 nodeid 删/重写该用例（升级点②定向修复），通过用例保留 |
| product_defect 高置信 | **冻结该用例**（它是缺陷的证人），证据链写入 test_reports.defects；不为它重生成；任务终态「发现 N 个疑似缺陷」是有效结果 |
| 低置信/模糊 | LLM 复审一次 → 仍模糊 → 人工标注门（feedback_annotations，确认门哲学的延伸） |

### 7.3 论文表述口径

诚实口径：系统输出的是「带证据链的疑似分类 + 置信度」，不声称全自动定性；最终定性由人工标注（feedback_annotations 表）与变异标定实验给出准确率。**归因正确率由此成为论文的一个可量化指标，而非口头承诺**——这正是"误报率实验"（feedback_annotations 设计初衷）的扩展。
