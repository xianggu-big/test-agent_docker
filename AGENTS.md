# 毕设：基于大模型 Agent 与容器技术的自动化软件测试方法研究

## 当前状态
- 目标：Test Agent 端到端自动化测试系统（LangGraph 编排 + Docker 沙箱 + SQLite 落库），服务论文与答辩
- 愿景六步流程（2026-10-26 用户定稿）权威记录在 docs/vision-and-gaps.md：repo+需求→语言占比→预构建镜像→生成用例+二次审查→R-Loop(3轮)→评估工具集出报告→报告二次审查回炉；总纲=四阶段（分析/设计/执行/评估）
- 愿景↔现状：主链路/确认门/归因三分类/指纹熔断/预构建镜像已通；缺口=plan节点+test_conditions(RTM)、用例六要素结构化、语义二次审查、评估五工具(collect_metrics/map_requirements/gen_report/review_report/gen_charts)、人工插用例(依赖L1)
- 已定口径：固定 LangGraph 图做骨架，LLM 的 plan 是节点产物(plan节点+gen_tests)；不承诺"直到成功"，product_defect 如实报缺陷不回炉用例；评估数字全由确定性代码从 SQL 算、LLM 只叙述；二次审查规则闸(AST/collect-only)先行、LLM 审为辅
- 五层架构：L1 界面（未建，cli.py 暂代）｜L2 graph/ 编排｜L3 skills/ 注册表（审计+确认门）｜L4 sandbox/ 容器执行｜L5 store/ 13 表 SQLite（拟加 test_conditions →14）
- 关键文件：docs/layer5-data-schema.sql（13 表）、docs/testing-theory.md（理论↔系统映射）、docs/vision-and-gaps.md、docker/ 两 Dockerfile（mta-test:py312 / jdk17-maven）、skills/prompt_library.py（五方法骨架）
- 已知口径风险：container_jobs 台账非真队列；analyze 单 module(path='.')；install_deps 只认根 requirements
- 坑/缺口：无 tests/ 目录（repair.py 标注"可单测"未落地）；非 git 仓库（未 git init）
- 2026-10-26 全量审计（清单=docs/audit-findings.md）：代码层 A1-A10，3 个高危=准出标准未真冻结（repair 读全局 config 非任务行）/超时后复用容器违反 runner 自定约定/provision 无干净容器保证（重跑残留旧用例）；构思层最大空洞 B1=实验设计方案缺位且 demo 缺 product_defect、env 两类场景
- 2026-10-26 loop 构思结论：loop 金标准=每轮有信息增量（学习环≠重试环）；升级点①②是实现用户"回炉重造"愿景的前提（否则指纹熔断第 2 轮必触发），建议直接纳入 v1
- ①覆盖缺口回灌：evaluate 只取 TOTAL（nodes.py:194），term-missing 缺口行被丢弃；应灌回 generate 提示词（CoverUp 模式）
- ②定向修复：gen_tests 每轮追加 test_gen_rN.py，坏用例残留→指纹重复，FINGERPRINT_BREAKER=2 下第二轮必熔断；应按 nodeid 删/重写坏用例，通过用例保留（TestGen-LLM 准入思想）
- ③RTM 缺测试条件层 → 用户 2026-10-26 愿景已确认，落点=plan 节点+test_conditions 表（vision-and-gaps.md §3.2）；④变异测试定位已定稿（§7）：归因标定仪器+有效用例率，实验=注入已知缺陷测归因混淆矩阵
- 归因设计定稿（2026-10-26，vision-and-gaps.md §7）：自动归因不可能 100% 准（oracle 问题）；证据分层打分+置信度，不确定时"宁可误报不可造假"；需求明示的失败用例永不删/不改断言；gen_tests 输出加 expected_basis 字段
- 文献锚点：ISTQB v4 测试过程（基线）、Meta TestGen-LLM、CoverUp、CodaMosa（前沿，各对应 loop 内一个机制）
- 2026-10-27 搭建计划获批（docs/build-plan.md）：阶段0已提交（21adace：A1-A12 清零、单测 24 绿、双场景冒烟过）；阶段1已提交（1166e0d：镜像自动构建闭环+性能对比实验，结果在 benchmarks/results/，复跑两次数据吻合）
- **实验核心结论（论文引用）**：容器热运行 0.8s vs Windows 宿主 3.1s（约3.8x），冷启动一次性 ~1.3s——容器化无性能 penalty 反而更快，L4 选型的量化依据；诚实边界=单机/n=5/微型项目/Linux 宿主上差距预计收窄
- 下一步：阶段 3（①覆盖缺口回灌+②定向修复，做完立即跑轮次轨迹验证；B2 用 test_reports.per_file JSON 列）→ plan/RTM 节点 → 阶段 4 评估五工具
- 阶段 2.1 已完成（bb9b6b7）：红队自测实锤并修复 5 缺陷——任务变砖无解卡（admin 强停）/跨脚本重名静默吞用例（补 def 名重写+全域查重）/enabled 装饰性（toggle 端点+过滤+前端接真）/前端 XSS（esc 全量转义，DOM 级验证通过）/孤儿容器清扫；scripts/api_adversarial.py 9 项全绿可复跑
- 阶段 2 已完成（81d5d86）：L1 界面按图上线，uvicorn 起服务 http://127.0.0.1:8000（演示账号 admin/admin123 · dev/dev123 · caller/caller123）；API 冒烟 15/15（scripts/api_smoke.py）；浏览器截图对照 mockup 三页一致；fastapi==0.162.0（starlette>=0.49 兼容本机 langgraph-api，勿降级）；IAB 自动化受信输入不达页面属环境怪癖（页面内事件验证全通过）
