## 测试报告

### 结论
通过 18、失败 1720、跳过 51、覆盖率 null%
终态：stop

### 四阶段回溯
**分析**：需求解析完成，RTM 中共识别 10 条测试项，全部为 gap 状态，无有效用例关联。
**设计**：基于等价类划分与 East Asian Width 属性构建测试场景，但未生成实际测试用例（cases: []），设计环节存在断层。
**执行**：运行测试阶段耗时 29170ms，共执行 0 个用例（cases_total: 0），所有用例均未触发，执行层面为空白。
**评估**：trace 轨迹显示第 1 轮即停止，失败数达 1720，通过率仅 0.0101。工具链整体耗时分布如下：
- collect_metrics: 31ms
- gen_charts: 1121ms
- gen_report: 5497ms
- gen_tests: 46740ms（成功率为 0.0）
- install_deps: 8466ms
- map_requirements: 37ms
- review_report: 806ms
- run_tests: 29170ms
综合评估显示测试执行未覆盖任何实际场景，流程中断于早期。

### 缺陷清单
failure_kind=product_defect：疑似产品缺陷：断言失败用例保留为证人，如实上报缺陷清单（不回炉迁就）

### 风险与建议
RTM gap 条件共存在 10 条，其中全部条件均无对应测试用例，复杂度热点未覆盖 5/5。无 LLM 成本数据；无复杂度热点数据