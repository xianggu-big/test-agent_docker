# 全量审计：逻辑漏洞 + 功能漏洞 + 构思缺口（2026-10-26）

> 用途：修复工作清单。与 vision-and-gaps.md（愿景/协同设计）、AGENTS.md「当前状态」三件套配套。
> 严重度：🔴 高（影响实验数据可信度或核心机制）｜🟡 中（功能缺陷）｜🟢 低（口径/打磨）

## A. 代码层功能漏洞

### A1 🔴 准出标准没有真正冻结进任务
- 位置：nodes.py:116（analyze 只在日志里"宣布冻结"）；repair.py:68,73,85（decide 每次读全局 config.COVERAGE_TARGET）
- 问题：config.py:5 自定"实验预注册纪律：预算值冻结"，但物理上没有冻结载体。实验中途改 config，历史任务与运行中任务的判定口径全部漂移——实验数据不可信。
- 修法：test_tasks 加 `coverage_target` 列（schema 13→14 时与 test_conditions 一起），analyze 写入，decide 从任务行读。

### A2 🔴 执行超时后容器被复用，违反 runner 自己写的约定
- 位置：runner.py:9-10 注释明说"超时任务由调用方 cleanup，不得复用"；但主流程：run 超时（nodes.py:173-174）→ evaluate → repair_env → provision → ensure_container 同名直接复用（runner.py:53-56）
- 后果：旧 exec 进程还在容器里跑，与新 pytest 争抢 1 核 CPU → 新一轮大概率再超时 → 一路熔断/预算耗尽，报告把"超时连锁"误写成归因结论。
- 修法：超时后置 rebuild 标记（TaskState 新字段或用 last_exit==-1 判定），provision 见标记先 cleanup 再新建。

### A3 🔴 重跑同一任务时容器内残留旧生成用例（与 A2 同根：provision 无"干净容器"保证）
- 位置：clone（nodes.py:69-70）rmtree 重建宿主工作区；但容器若未被回收（进程被杀/cleanup 失败），put_archive 只覆盖不删除 → 容器残留上一轮 test_gen_rN.py 与旧依赖 → 新一轮把旧生成文件也跑进去。
- 修法：provision 一律先 cleanup 再新建容器（依赖重装代价可接受），或容器内 rm -rf /work/* 再快照。建议与 A2 同一个改动解决。

### A4 🟡 任务级互斥缺失，同一任务可并发 run
- 位置：cli.py:65 / builder.run_task 无状态检查
- 后果：两次 run → 两个 graph 实例、同一 agent_sessions 流、同名容器（第二个复用第一个）→ exec 与消息交叉。
- 修法：run_task 开头查 status，处于 cloning~reporting 区间即拒绝；或 UPDATE ... WHERE status='queued' 抢占式置位做乐观锁。

### A5 🟡 用例名与 pytest nodeid 映射断裂（升级点②动工的前置修复）
- 位置：gen_tests.py:106-108 落库 name=LLM 自报 name 字段；nodeid 取自 code 里实际 def 名（gen_tests.py:60 只校验 def test_ 存在，不校验一致）
- 后果：两者不一致时，evaluate 的指纹/归因拿到的 nodeid 在 test_cases 查不到 → ②定向修复（按 nodeid 删改坏用例）整个机制建立不起来。
- 修法：写文件前把 def 名重写为 name 字段（后处理正则），或落库 name 改为从 code 提取的 def 名。

### A6 🟡 test_scripts.module_id 恒为 NULL
- 位置：gen_tests.py:99-103 INSERT 未传 module_id；generate 节点也没把 module_id 放进 args（nodes.py:149-154）
- 后果：schema 的 UNIQUE(task_id, module_id, version)、报告关联、论文"模块维度指标"都建立在它上。
- 修法：generate args 加 module_id，落库补上。

### A7 🟢 Java 占位链路的停止原因语义失真
- 位置：nodes.py:178-179 占位失败 → classify→"env" → repair_env → 第 2 轮同指纹熔断 → stop_reason"Agent 原地打转"
- 修法：cmd=None 时直接 done=True + 诚实 stop_reason（"Java 执行链路 v1 未接入"），不进归因循环。

### A8 🟢 coverage 解析失败按达标放行
- 位置：repair.py:68 `coverage_line is None or ...`
- 后果：TOTAL 行缺失/格式变化 → None → 测试全过即 pass，覆盖率核查静默失效。
- 修法：None → 不达标 → repair_gen（理由"覆盖率未知"），受轮次预算与熔断兜底。

### A9 🟢 .gitignore 整目录忽略 data/，演示数据不进版本库
- 位置：.gitignore:2 `data/`；data/demo/ 是答辩与实验数据集（场景A=case_defect 演示、场景B=全过、llmdemo=生成演示）
- 修法：改忽略 `data/workspaces/`、`data/*.db`，保留 data/demo/。git init 时一并处理。

### A10 🟢 碎项合集
- _SKIP_DIRS 两处定义不一致（nodes.py:27 含 .idea，gen_tests.py:24 不含）→ 收拢到 config
- install_deps 只认根 requirements.txt（monorepo 不支持，论文口径写明）
- container_jobs 不写 priority/timeout_at（台账语义不完整）
- msg() seq 用 COUNT(*)，并发写有竞态（单任务串行下无害，A4 修复后彻底无虞）
- CLI 未配置 logging（cli.py 无 basicConfig，模块 logger 的 info 全部丢弃，warning 走 lastResort）→ main 里加一行

### A12 🟡 指纹塌缩：所有失败共享同一指纹（2026-10-27 单测抓到，已修复）
- 位置：repair.py `fingerprint_of` 原正则 `(\S+?)` 非贪婪且无行尾锚点 → 每行只匹配 1 个字符，所有 FAILED/ERROR 行的 nodeid 塌缩成同一值 → 任意两轮失败（哪怕失败点完全不同）都触发同指纹熔断，熔断语义失真。
- 发现方式：tests/test_repair.py `test_不同用例失败_异指纹`（阶段 0 新增单测的即时回报——"纯函数可单测"注释欠下的债）。
- 修复：改为按 " - " 分隔符解析 nodeid 与错误类名。

## B. 构思层未完善

### B1 🔴 实验设计方案完全缺位（构思里最大的空洞）
论文要跑实验，但未定义：实验对象集（用哪些被测项目）、对照组设计（有无 R-Loop？有无五方法骨架？预构建 vs 现场构建？）、指标采集口径（哪张表出哪个数）。且 demo 数据集只有两类场景：场景B（全过）、场景A（case_defect：div(4,2)==5）。**缺 product_defect 场景（带真 bug 的 calc，如 return a-b）与 env 失败场景**——归因三分类的实验数据缺一半；变异标定实验（vision-and-gaps.md §7.1-5）的 mutant 集也没建。

### B2 🟡 覆盖率明细未留存
evaluate 只留总量；term-missing 逐文件缺口行被丢——升级点①（缺口回灌）没有数据基础，论文画"覆盖率随轮次趋势"也需要 per-file 数据落库（建议 test_reports 加 per_file JSON 或新表 coverage_details）。

### B3 已定稿待实现（不重复展开，见 vision-and-gaps.md）
plan/RTM 节点+test_conditions（§3.2）、六要素结构化（§3.3）、评估五工具（§3.1/§3.4/§3.5）、归因证据分层+expected_basis（§7.1）、语义二次审查（§3.6）、人工插用例（L1）、升级点①②。

## C. 建议修复顺序

1. **A5 → A1 → A2+A3 → A4**（核心机制与实验可信度；A2/A3 一次改动解决）
2. **A6、A7、A8、A10**（半天内可清完的小项）
3. **A9 + git init**（先改 .gitignore 再 init，demo 数据才进得了库）
4. 之后按 vision-and-gaps.md §4 推进：①② → plan 节点 → 评估五工具 → L1；B1 实验设计最好在评估五工具动工前想清楚（工具的输出格式由实验指标需求倒推）。
