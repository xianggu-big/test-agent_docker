# B1 实验设计定稿（论文实验章蓝图）

> 状态：2026-10-28 定稿。本文是全部实验的权威设计；AGENTS.md 只留结论行。
> 原则（对齐 vision-and-gaps.md §3）：评估数字全由确定性代码从 SQL/pytest 输出算出，LLM 只叙述；
> 每个实验写明诚实边界（n、环境、随机性），答辩被质疑时以本文口径回答。

## 0. 研究问题（RQ）与实验映射

| RQ | 问题 | 实验 | 状态 |
|---|---|---|---|
| RQ1 | 容器化沙箱执行是否引入性能惩罚？ | E1 容器 vs 宿主对比 | ✅ 完成（benchmarks/results/bench_*.md） |
| RQ2 | LLM 生成用例失败后的修复策略哪种更优？ | E2 三链路受控对比 | ✅ 完成（pipeline_compare_*.md） |
| RQ3 | R-Loop 是否构成"学习环"（每轮有信息增量）而非"重试环"？ | E3 轮次轨迹分析 | ✅ 单点轨迹已落库（场景A/C），趋势图由 gen_charts 出 |
| RQ4 | 归因器（env/case/product 三分类）准确率多少？ | E4 变异标定混淆矩阵 | ✅ 完成（14/14，benchmarks/results/mutation_calibration_*.md） |
| RQ5 | 报告二次审查能否拦住"数字与数据不符"的叙述？ | E5 审查双闸注入实验 | ✅ 完成（22b3d2e 五工具+review 节点） |

## 1. E1 性能对比（已提交 1166e0d）

- **设计**：同被测对象（场景A/B），冷=建容器+快照+装依赖+执行，热=复用容器仅重跑 pytest，宿主组=Windows subprocess 直跑；各 5 轮报均值±标准差，分段计时。
- **结论**：容器热运行 0.8s vs 宿主 3.1s（≈3.8x），冷启动一次性 ~1.3s。
- **诚实边界**：单机 / n=5 / 微型项目 / Windows 宿主；Linux 宿主上差距预计收窄（写入论文局限节）。
- **论文落点**：L4 选型的量化依据（第4章）。

## 2. E2 三链路对比（已提交）

- **受控变量**：同一被测对象（场景E/cart.py）、同一合成第 1 轮生成物（2 好 + 2 强信号坏 + 1 断言证人）、同一冻结准出标准 0.70；真实环节 = Docker 执行 + DeepSeek 修复/生成；机制层差异由 fake-LLM 单测锁死（tests/test_repair_strategy.py）。
- **自变量**：定向修复策略 ∈ {A=prune 剪除+全量重生成, B=rewrite(YATE 带上下文重写), C=hybrid 合成}。
- **因变量**：修复耗时、LLM 调用数、第 2 轮通过/失败、覆盖率、证人保留、端到端耗时。
- **结论**：A 11.9s/100%（确定性最强、重生成 19 条成本高）；B 3.5s/97%（最快但不补覆盖缺口）；C 13.1s/100%（A 的缺口兜底 + B 的种子保留，成本换鲁棒）。三链路证人均保留、均正确停 product_defect。
- **诚实边界**：每链路 n=1（LLM 随机性）；趋势由机制单测 + E4 共同支撑。
- **论文落点**：方法对比节 + 讨论节（优缺点素材已生成在 results md）。

## 3. E3 R-Loop 轮次轨迹

- **数据源**：agent_messages 中 `[evaluate 第N轮]` 结构化消息（通过/失败/覆盖率/指纹/verdict），test_reports 多行（每轮终态一行）。
- **指标**：覆盖率随轮次趋势（gen_charts 趋势图）；每轮信息增量 = 缺口行数递减 / 失败集合变化（指纹不同 ≠ 原地打转）。
- **已有证据**：场景A 第 1 轮 27 过 1 败即停（证人）；场景C 10 过 7 败即停（真 bug 如实上报）。
- **论文落点**：R-Loop 设计有效性（第4章 + 插图）。

## 4. E4 变异标定实验（归因混淆矩阵，已完成 14/14）

- **目的**：归因器 `repair.classify_failure` 的准确率首次获得可控真值——不注入缺陷时无从知道"疑似 product_defect"判对了没有。
- **设计**：
  - **真值构造**：mutant 集 = 对场景E/cart.py 施加变异算子（边界翻转 / 条件取反 / 运算符替换 / 返回值偏移，≥8 个存活 mutant）；case 集 = 预置已知坏用例（预期值翻转 / 错误 import）。
  - **受控**：固定测试套件（含断言失败证人 + 正常用例），只变被测对象；不调 LLM（归因是确定性启发式，实验测的就是它本身）。
  - **流程**：每个 mutant/坏用例 → 临时工作区 → Docker 容器执行 pytest → 解析输出 → classify_failure 归因 → 与真值比对。
  - **混淆矩阵**：3×3（真值 env/case/product × 预测 env/case/product），另报准出判定的"该停没停/不该停停了"两类错误——后者对防造假语义更致命。
- **预期**：强信号类（ImportError/AttributeError）→ case 全对；断言失败 mutant → product（因为预期值来自需求/docstring 的用例断言失败）；已知风险 = 源码推导预期值撞上 mutant 可能漏报（oracle 问题的实证，论文引用 Barr 综述）。
- **运行**：`python benchmarks/mutation_calibration.py`（需 Docker）。
- **论文落点**：§7.3 口径"归因正确率成为可量化指标"的数据来源（第5章实验 + 混淆矩阵表）。

## 5. E5 报告二次审查双闸（已完成）

- **目的**：评估"数字全部来自 SQL"的纪律下，叙述层仍可能失真（LLM 复述错数字 / 归因 product_defect 却写已解决）；审查闸是最后防线。
- **设计**：规则闸（确定性先行：覆盖率越界 [0,1]；叙述数字 vs 指标包逐项比对；product_defect 终态却含"已解决/全部通过"）+ LLM 闸（叙述与数据一致性，无 Key 诚实跳过）。
- **注入实验**：构造 5 类坏报告（数字篡改×2、越界覆盖率、矛盾终态、合规对照）→ 审查应 reject 前 4 类、pass 对照；回炉语义 = reject 且 verdict=pass 时 verdict=repair_gen 吃同一轮次预算。
- **运行**：`pytest tests/test_review_node.py tests/test_eval_tools.py`（机制锁死）+ 端到端冒烟观察轨迹。
- **论文落点**：评估阶段闭环设计（第4章）+ 拦截率数字（第5章）。

## 6. 指标定义（论文口径，collect_metrics 的输出契约）

| 指标 | 定义 | SQL 来源 |
|---|---|---|
| 用例通过率 | passed / (passed+failed+skipped) | test_reports 最新行 |
| 行覆盖率 | pytest --cov TOTAL% | test_reports.coverage_line |
| RTM 条件覆盖数 | status=covered 的条件数 / 条件总数 | rtm_maps.matrix |
| 六要素完整率 | expected 非空用例数 / 用例总数 | test_cases |
| 工具调用成功率 | status=ok / 总调用 | skill_invocations |
| 归因分布 | 每轮 failure_kind 计数 | agent_messages(evaluate) |
| 阶段耗时 | 各 skill duration_ms 汇总 | skill_invocations |
| 归因混淆矩阵 | 见 E4 | benchmarks/mutation_calibration.py 输出 |

## 7. 诚实边界汇总（论文"威胁 to validity"节素材）

1. n=1 或 n=5 的小样本：LLM 随机性 + 单机环境；报告均值±标准差，不报单值。
2. 微型被测对象（≤50 行）：结论外推到大仓库存疑，列为未来工作。
3. 归因启发式的 oracle 边界：混淆矩阵只证明"在已知真值集上"的准确率。
4. 演示需求为中文自然语言：plan/RTM 拆解质量依赖 LLM 中文能力，未做多语言对比。
5. 网络依赖：DeepSeek API 可达性影响端到端实验复现，机制层结论有 fake-LLM 单测兜底。
