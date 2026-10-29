# Test Agent：基于大模型 Agent 与容器技术的自动化软件测试系统

毕设项目。LangGraph 固定图编排 + Docker 沙箱执行 + SQLite 落库，端到端完成
「需求 → 测试条件（RTM）→ 用例生成 → 容器执行 → 失败归因 → 补测循环 → 评估报告」全流程。

## 架构（五层）

```
L1 界面    FastAPI + 原生前端（uvicorn 起服务 http://127.0.0.1:8000）
L2 编排    graph/ LangGraph 固定图（9 节点）：
           clone → analyze → plan(RTM) → provision → generate → run → evaluate → report → review
                                                     ↑________ repair_env / repair_gen（R-Loop 回炉）__↑
L3 技能    skills/ 注册表：schema / 风险分级 / 超时 / 审计台账 / fail-closed 确认门
           内置：install_deps / run_tests(high,需确认) / gen_tests + 评估五工具（evaluate.py）
L4 沙箱    sandbox/ Docker 容器执行（mta-test:py312 镜像，非 root tester + 资源限额）
L5 数据    store/ 18 张表 SQLite（WAL + 外键 + 幂等迁移）：docs/layer5-data-schema.sql
```

## 核心机制

- **R-Loop 补测循环（三停）**：准出达标即停 / 同指纹熔断（≥2 次原地打转）/ 轮次预算耗尽如实上报。
  不承诺"直到成功"——product_defect 是产品真缺陷，重造用例迁就 = 系统造假。
- **失败归因三分类**：env（修环境）/ case_defect（剪坏用例）/ product_defect（断言失败证人冻结，如实上报）。
  归因准确率由变异标定实验给出可控真值（14/14，见 benchmarks/results/mutation_calibration_*.md）。
- **定向修复三策略**（config.REPAIR_STRATEGY 一行切换）：A=prune 剪除+全量重生成（默认）/
  B=rewrite 带失败上下文重写（YATE）/ C=hybrid 合成（B 保留种子 + A 缺口回灌兜底）。
  受控对比数据：benchmarks/results/pipeline_compare_*.md。
- **评估五工具**（数字全部 SQL 确定性计算，LLM 只叙述）：collect_metrics / map_requirements(RTM) /
  gen_report（模板固定、数字禁造）/ review_report（规则闸先行+LLM 闸，reject 仅推翻 pass 判定回炉）/
  gen_charts（matplotlib 四图：RTM/覆盖率趋势/判定分布/工具耗时）。
- **诚实性设计**：用例六要素 + expected_basis（预期值出处）落库；需求明示的失败用例永不删、永不为它改断言；
  报告数字与指标包逐项比对（防造假红线：product_defect 却写"已解决"直接 reject）。

## 快速开始

```bash
pip install -r requirements.txt -r requirements-dev.txt
python scripts/build_images.py            # 预构建沙箱镜像（首次约 2 分钟）
set DEEPSEEK_API_KEY=sk-...               # LLM Key（无 Key 时全链路诚实降级，仍可跑）

uvicorn test_agent.api.server:app --host 127.0.0.1 --port 8000
# 浏览器 http://127.0.0.1:8000  演示账号 admin/admin123 · dev/dev123 · caller/caller123
```

CLI 等价链路（答辩演示/脚本化）：

```bash
python cli.py new <本地目录或git地址> "需求描述"   # 建任务
python cli.py confirm <task_id>                   # 人工确认门（fail-closed）
python cli.py run <task_id>                       # 下发执行（R-Loop 全链路）
python cli.py report <task_id>                    # 报告（标注最新）
python cli.py log <task_id>                       # Agent 会话轨迹（按会话分组）
python scripts/demo.py                            # 一键演示：建任务→确认→执行→摘要
```

## 实验复现（论文第 5 章）

| 实验 | 命令 | 结果 |
|---|---|---|
| E1 容器 vs 宿主性能 | `python benchmarks/bench_local_vs_docker.py` | benchmarks/results/bench_*.md（热 0.8s vs 3.1s，≈3.8x） |
| E2 三链路修复策略对比 | `python benchmarks/compare_pipelines.py` | benchmarks/results/pipeline_compare_*.md |
| E3 R-Loop 轮次轨迹 | 跑任意任务后 `python cli.py report <tid>` | gen_charts 趋势图 data/reports/charts/ |
| E4 归因变异标定 | `python benchmarks/mutation_calibration.py` | benchmarks/results/mutation_calibration_*.md（14/14） |
| E5 报告审查双闸 | `pytest tests/test_review_node.py tests/test_eval_tools.py` | 机制锁死 + 端到端轨迹 |
| X 格消融（X1-X7/A1-C1） | `python scripts/run_grid.py <格名> [corpus_id]` | 摘要 stdout + token 成本行 |
| 语料阅卷 + 聚合 | `python scripts/grade_corpus.py <tid> <corpus>` → `python scripts/aggregate_grid.py` | data/corpus/*/grade_task*.json；聚合表含 recall±std + token 成本 |
| 阅卷协议修正重算 | `python scripts/grade_corpus.py --recompute <corpus_id>` | 沿用原仲裁判决离线重算（幂等） |
| 仲裁人工抽查 | `python scripts/spot_check.py [--rate 0.2]` | 固定种子 20% 复核清单（stdout） |
| 机制面完成度巡检 | `python scripts/module_check.py [--full]` | data/reports/module_check_*.json |
| 三方案独有指标（弱断率/杀死率/RTM覆盖） | `python scripts/mutation_kill.py <tid> <corpus>` → `python scripts/compare_proposals.py` | data/corpus/*/mutation_task*.json；跨格聚合表 stdout |
| L2 集成层冒烟（四级双轨 P1） | `python scripts/smoke_l2.py [demo_dir] [--keep-ws]` | 真机 11 过/1 败（埋的缺陷被顺序场景如实抓到）；leg 台账 test_levels |
| L2 全链路接力（L1→L2） | `python -c "from test_agent.graph.levels import run_task_levels; run_task_levels(<tid>)"`（需 LEVELS 场景自建任务） | test_levels 两行 + trace_from 链 + evaluation_rounds 按级 |

回归：`python -m pytest`（249 过 + 1 e2e 慢测默认跳过，`MTA_E2E=1` 启用）；
API 冒烟 `python scripts/api_smoke.py`（15 项）；对抗 `python scripts/api_adversarial.py`（9 项）。

## 文档索引

- `docs/vision-and-gaps.md` —— 愿景六步流程权威记录 + 设计定稿（归因 §7、评估 §3）
- `docs/build-plan.md` —— 阶段 0-4 搭建计划（已全部完成）
- `docs/experiment-design.md` —— 实验设计定稿（RQ1-5 ↔ E1-E5 映射）
- `docs/layer5-data-schema.sql` —— 数据层 18 表 DDL（单一真源，建库幂等）
- `docs/testing-theory.md` —— 测试理论 ↔ 系统机制映射
- `docs/industry-testing-research.md` —— 主流软测流程调研（33 信源逐条核验）
- `docs/audit-findings.md` —— 全量审计清单（A1-A13 已清零）
- `docs/pitfalls-python.md` —— Python 工程踩坑记录
