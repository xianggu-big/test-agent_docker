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
L5 数据    store/ 28 张表 SQLite（WAL + 外键 + 幂等迁移）：docs/layer5-data-schema.sql
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
- **四级双轨（测试级别）**：L1 单元（默认深做）/ L2 集成（契约+调用链双轨）/ L3 系统半自动（冒烟自动判定+
  失败人工介入）/ L4 验收辅助（GWT 场景确认+确定性转化回归）。级别开关默认关（向后兼容），
  由级别编排器串行接力并以 `test_levels.trace_from` 形成跨级追溯链；`GET /api/tasks/{tid}/trace`
  输出"验收→系统→集成→单元→代码位置"确定性溯源链（零 LLM）。

## 快速开始

```bash
pip install -r requirements.txt -r requirements-dev.txt
python scripts/build_images.py            # 预构建沙箱镜像（首次约 2 分钟）
# 回归基线数字不手写：`python scripts/module_check.py`（守卫+完成度）与 `python -m pytest -q` 为唯一真源
set DEEPSEEK_API_KEY=sk-...               # LLM Key（无 Key 时全链路诚实降级，仍可跑）

uvicorn test_agent.api.server:app --host 127.0.0.1 --port 8000
# 浏览器 http://127.0.0.1:8000  演示账号 admin/admin123 · dev/dev123 · caller/caller123
```

### 语言支持分级（2026-11-01）

| 级别 | 语言 | 能力 | 镜像 |
|---|---|---|---|
| Tier1 全链路 | Python | 五法生成 → 三道闸 → 容器 pytest → 覆盖率 → R-Loop 归因/证人 → 评估五工具 → 四级接力 | `mta-test:py312` |
| Tier2 执行链路 | JavaScript | jest 用例生成 → node --check 闸 → 容器 jest → 覆盖率 → R-Loop 归因（jest 输出经 `js_adapter` 转 pytest 文本，下游零改动） | `mta-test:node20-jest` |
| Tier3 静态模式 | 其余（Java/TS/Go/C…） | 语言无关确定性扫描（符号/风险词/空 catch/超长函数/深嵌套）+ AI 测试设计草稿（六要素落库，`framework=static_draft`）；**不执行、无覆盖率，报告明示未验证** | 无需容器 |

诚实边界：TypeScript 的 jest transform 需项目级 ts-jest 配置，全局镜像跑不了 → 暂列 Tier3；
Java 镜像（`mta-test:jdk17-maven`）已备但 JUnit 生成/surefire 解析链路未接 → Tier3；
JS 两阶段评审的"勾选停用"只落台账、暂不摘除 jest 文件内的用例（无 JS AST 摘除器）。

### Windows 桌面一键启动

已提供桌面启动器（`scripts/start_ui.ps1`）：

1. 双击桌面 **「Test Agent」** 图标；
2. 弹窗输入 **DeepSeek API Key**（仅本次进程使用，不落盘；只看已有报告可点“仅看界面”）；
3. 自动检测并启动 **Docker Desktop**，等待 daemon 就绪；
4. 自动启动服务并打开 **http://127.0.0.1:8000**；
5. **关闭启动器控制台窗口 = 停止服务**（也可 Ctrl+C）。
6. 如果端口已有本项目的旧服务，会弹窗询问：**停止旧服务并重新输入 Key**，或直接打开现有界面。

首次/重建快捷方式：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/create_desktop_shortcut.ps1
```

配置：`scripts/launcher.config.json`（端口、Docker Desktop/CLI 路径、Docker 等待秒数、是否弹 Key、启动前命令）。
“拉取任务”等启动前动作填到 `pre_start_commands`，例如：

```json
"pre_start_commands": [
  { "command": "powershell -File C:\\path\\pull_tasks.ps1", "working_dir": "C:\\path" }
]
```

诊断（不启动服务/不弹窗）：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/start_ui.ps1 -CheckOnly
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
| L3 系统半自动 | 同上，`LEVELS_L3_ENABLED=True` 且 levels 含 L3 | 场景三态台账 + 冒烟结果（失败转人工确认） |
| L4 验收辅助 + D6 转化 | 同上，`LEVELS_L4_ENABLED=True`；确认后 `POST /api/tasks/{tid}/l4/convert` | L4 21 场景→确认 2→转化 2 个 pytest 函数 |
| 跨级溯源链 | `GET /api/tasks/{tid}/trace[?scenario_id=]`（或 `python scripts/backfill_callgraph.py <tid>` 回填调用图） | 验收→系统→集成→单元→代码位置（函数级候选） |
| corpus-003 缺陷标定 | `python scripts/build_corpus.py validate data/corpus/corpus-003` → `python scripts/calibrate_corpus.py corpus-003` | 5 真缺陷可暴露 + 2 decoy 不暴露 |
| E7 跨级溯源跑数 | `python scripts/run_e7_corpus003.py --n 3` | data/corpus/corpus-003/e7_task_*.json（402 时 blocked 不计均值） |

回归：`python -m pytest`（320 过 + 1 e2e 慢测默认跳过，`MTA_E2E=1` 启用）；
API 冒烟 `python scripts/api_smoke.py`（15 项）；对抗 `python scripts/api_adversarial.py`（9 项）。

## 文档索引

- `docs/vision-and-gaps.md` —— 愿景六步流程权威记录 + 设计定稿（归因 §7、评估 §3）
- `docs/build-plan.md` —— 阶段 0-4 搭建计划（已全部完成）
- `docs/experiment-design.md` —— 实验设计定稿（RQ1-5 ↔ E1-E5 映射）
- `docs/layer5-data-schema.sql` —— 数据层 22 表 DDL（单一真源，建库幂等）
- `docs/testing-theory.md` —— 测试理论 ↔ 系统机制映射
- `docs/industry-testing-research.md` —— 主流软测流程调研（33 信源逐条核验）
- `docs/audit-findings.md` —— 全量审计清单（A1-A13 已清零）
- `docs/pitfalls-python.md` —— Python 工程踩坑记录
- `docs/四级双轨测试框架研究规划.md` —— 四级双轨方向定稿蓝图（D1-D10 决策）
- `docs/项目进度时间表.md` —— 里程碑与周排期（学校硬节点兜底）
- `docs/论文写作蓝图.md` / `docs/论文草稿/` —— 论文写作计划与章节草稿
