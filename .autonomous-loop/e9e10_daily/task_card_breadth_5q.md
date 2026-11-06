# 任务卡：测试广度增强「15+5 双轨采样」（2026-11-06 用户拍板方案 A）

## 时序（硬约束）

1. **先跑完当前 E9 波次**（346 格档：A1 102 / A2 90 / A3 54 / A5 100，旧口径，跑完出聚合报告）
2. 波次结束、波次锁释放后 → 实施本卡改动（平台代码，BREADTH_SAMPLING_ENABLED 默认关）
3. 实施 + 单测 + 回归后 → run_grid X-BREADTH 对照格 n≥3 验证
4. 判据：条件覆盖函数域数上升 + 检出（证人）不降 → 有效再议后续波次默认开；无效如实写负结果

## 改动清单（~150 行内）

1. `test_agent/skills/prompt_library.py`：plan 提示词注入源码结构清单（复用 _collect_sources
   枚举）+ 拆解纪律「条件须覆盖清单中大部分函数域；同函数域 ≤3 条」（信息提供，非行为阻止）
2. `test_agent/skills/gen_tests.py` L1175-1185 降采样：BREADTH_SAMPLING_ENABLED（默认关）开时
   「15 优先级 + ≤5 广度」——保 15 后检测函数域覆盖，从被漏模块/函数各补 1 条，总 ≤20 条
   （E2E 实锤 20 条 = DeepSeek 稳定输出上限）
3. `scripts/run_grid.py` GRID 加 X-BREADTH 格（开关对照，XA/X9 同款）
4. 单测：降采样两态（关=旧行为逐字节 / 开=广度补充正确性）

## 调研锚点（已查）

- TestGen-LLM（Meta, 2411.07315）：uncovered class/method 优先生成——「未覆盖函数优先」同源
- 本项目同构件组合：SUT_MANIFEST（B2 函数清单注入）+ COND_FEEDBACK（条件缺口回灌）
- 防御矩阵第一原则：prompt 提供信息（信息缺口类）+ 确定性闸兜底（采样偏置类）

## 红线

- 默认关=运行中波次逐字节不变（口径零污染）
- 不动已跑 54 格与实验数字；X-BREADTH 结果负也如实写
