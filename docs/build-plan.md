# 基础设施搭建计划（已并入 2026-10-27 评审修订版，已获批）

> 状态：已全部完成（阶段 0-4 于 2026-10-28 收官）。执行顺序 = 阶段 0→1→2→3→4，每阶段两道门：**端到端冒烟（cli 过场景）+ git commit**。
> 配套文档：audit-findings.md（A 项编号出处）、vision-and-gaps.md（§3/§7 设计定稿）、AGENTS.md（当前状态）。

## 阶段 0：地基修复（A1-A11 清零 + git + 单测，约 1 天）
1. A5（②的前置）：gen_tests 写文件前重写 def 名 = name 字段，保证 test_cases.name 与 pytest nodeid 一一对应
2. A1：test_tasks 加 coverage_target 列——**layer5-data-schema.sql 同步改**（防 schema 文档漂移）；db.py 加轻量迁移（PRAGMA table_info 查缺列则 ALTER）；analyze 冻结进任务行；repair.decide 改收 target 参数
3. **A11（评审新增实锤）**：db.py 文档串声称 WAL 但从未设置——get_conn 补 `PRAGMA journal_mode=WAL` 与 `busy_timeout=5000`（阶段 2 并发读写的前置保障）
4. A2+A3：SandboxRunner.ensure_container 加 fresh 参数；run_task 开头预清理容器（重跑残留）；run 超时（last_exit==-1）后 provision 重建容器；普通 R-Loop 轮次仍复用（保依赖缓存）
5. A4：run_task 用 UPDATE...WHERE status 抢占式置位乐观锁，抢不到即拒绝并发下发
6. A7：Java 占位 run 节点直接 done=True 诚实终止（builder 加 run→report 条件边），不再进归因循环
7. A8：coverage 解析 None → 不达标 → repair_gen（"覆盖率未知"），受轮次预算与熔断兜底
8. A6：generate 节点把 module_id 传入 gen_tests，test_scripts 落库补上
9. A10：_SKIP_DIRS 收拢 config（含 .idea）；CLI 加 logging.basicConfig；container_jobs 补 priority/timeout_at
10. A9 + git：.gitignore 改为只忽略 data/workspaces/、data/*.db*（保留 data/demo/）→ git init → baseline 提交（修复前快照，答辩展示演进）
11. requirements-dev.txt（pytest 钉版本）+ tests/test_repair.py（fingerprint/classify/decide 全分支）

## 阶段 1：Docker 自动化 + 性能对比实验（约半天）
1. config 加镜像注册表 dict；SandboxRunner.ensure_image()：本地缺镜像自动 build（国内网络走 Dockerfile 现成镜像源 ARG）
2. scripts/build_images.py：一键构建/自检两个镜像（"自动拉取容器"闭环）
3. benchmarks/bench_local_vs_docker.py：
   - 被测对象：data/demo/场景A 与 场景B（评审修订：两个都跑）
   - **口径写死**：冷 = 建容器+快照+装依赖+执行；热 = 复用容器（依赖已缓存）只重跑 pytest；本地组 = 宿主 subprocess 直接 pytest
   - 各 5 轮，**报均值±标准差**（不报单值）
   - **对等性声明**：宿主需装 pytest+pytest-cov（requirements-dev 补齐），写进结果报告防质疑
   - 分段计时：建容器/快照/依赖/执行/端到端；输出 benchmarks/results/bench_*.md（论文直接引用）
4. 可选加分：--cpus 0.5 vs 2.0 资源限额对比（时间允许才做）

## 阶段 2：L1 界面按图 1:1 实现（约 2 天，风险最高）
技术栈：FastAPI + 静态 HTML/CSS/原生 JS 单页（无 node 构建链；选型见跳过项报告第 1 条）。新增 requirements：fastapi/uvicorn（钉版本）。
- 后端 test_agent/api/：REST 接真实数据层——POST /api/tasks、confirm、run（后台线程跑 graph+前端轮询；**开发期不用 uvicorn --reload 跑真任务**，双进程会开两个 graph）、GET /api/tasks、/report、/logs、GET/PUT /api/scripts（人工插用例 **name 落库走 A5 同款 def 名规范**，source='imported'）、GET /api/images+pull、GET/PUT /api/config；登录接真哈希+admin/developer/caller 三角色路由守卫
- 前端按图排版：左侧边栏（logo+五菜单+角色徽章蓝/绿/红）、六步流程条、新建测试页（地址+需求+生成按钮+右侧可编辑用例列表）、脚本工作台（固定框架+用例插件勾选+脚本预览+确认提示条）、任务列表（全部/运行中/已完成/失败 tabs+彩色状态徽章+进度条+未确认禁用下发+停止/重试/日志）、报告页（通过率徽章+总数/通过/失败/跳过四卡+概览/步骤/日志/附件 tabs+用例级通过/失败列表+导出）、设置页（基础配置+容器/镜像管理+插件管理+用户权限仅管理员）；蓝色卡片风格与图一致
- **纪律：主链路（新建→列表→确认→下发→报告）真接口，其余壳；超支立刻壳化，不许挤压阶段 3**（①②是论文创新核心）

## 阶段 3：核心机制升级（约 2-3 天）
①覆盖缺口回灌（evaluate 解析 term-missing 逐文件缺口 → generate 提示词注入）→ ②定向修复（按 nodeid 删/重写坏用例，通过用例保留）
- **评审纪律：①②做完立即用 demo 场景跑一次轮次轨迹验证**（学习环 vs 重试环的直观证据），轨迹数据落库喂论文"覆盖率随轮次趋势"图
- **B2 落库形态评审已定**：test_reports 加 per_file JSON 列（先不拆 coverage_details 表，阶段 4 需要再拆）
→ gen_tests 六要素+expected_basis → 生成后静态审查（AST+容器内 pytest --collect-only）→ plan 节点+test_conditions 表（RTM；迁移走 A1 同款机制）

## 阶段 4：评估五工具 + 实验（约 2-3 天）
B1 实验设计定稿（模板供用户填）→ collect_metrics → gen_report → review_report（双闸+回炉，吃轮次预算）→ map_requirements → gen_charts → 变异标定实验（mutant 集+归因混淆矩阵）

## 收尾：scripts/demo 演示链路 + README + AGENTS.md/经验文档更新

## ⚠️ 跳过项报告（重大不确定项，已按用户要求跳过并提醒）
1. **UI 技术栈**：定 FastAPI+原生前端（零 node 依赖、Python 单栈、答辩演示稳）。想换 React/Vite 视觉稿可平移，需装 node——动工前说一声即可换
2. **多测试框架**：图里有 Playwright/Jest/Postman，v1 只支持 pytest——UI 按图保留选项但置灰标"规划中"
3. **Java 执行链路**：维持占位+诚实化（A7），v1 不接真链路
4. **鉴权**：简单登录+三角色路由守卫（pbkdf2），不做 OAuth/JWT
5. **LLM 供应商**：维持 DeepSeek，config 可换 base_url/model
6. **部署**：本机演示（127.0.0.1:8000），不做服务器/容器编排
7. **报告页图表**：CSS 条形图内嵌+matplotlib 出 PNG（论文插图），不做交互式前端图表库

## 总工作量：约 8-11 个工作日
