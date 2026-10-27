# Python 线踩坑记录（pitfalls）

> 用途：Python 深度支持链路搭建过程中遇到的真实问题登记。**Java 线开工前必读**，
> 每条按「现象 → 根因 → 解法 → Java 线启示」四段记录，避免重复踩坑。
> 纪律：新坑随时追加；解法改动如果涉及代码，注明文件位置。

---

## 0. 环境快照（2026-10-27 实测通过版本，勿随意升级）

| 组件 | 版本 | 说明 |
|---|---|---|
| Python（宿主） | 3.13.9（Anaconda） | Agent 编排层运行环境 |
| Docker Desktop | 29.3.1 | 本地运行，非云服务器 |
| langchain | 1.2.15 | requirements.txt 已钉死 |
| langgraph | 1.1.6 | 状态机框架 |
| langchain-openai | 1.1.13 | DeepSeek 走 OpenAI 兼容接口 |
| docker SDK | 7.2.0 | L4 容器驱动 |
| 镜像 mta-test:py312 | pytest 9.1.1 / coverage 7.17.0 / ruff 0.17.2 / bandit 1.9.4 | 工具链锁在镜像里 = 实验可复现 |
| DeepSeek API | deepseek-chat，Key 走环境变量 DEEPSEEK_API_KEY | 不进代码库 |

---

## 1. PowerShell 引号转义，内联 Python 命令跑不了

- **现象**：`python -c "..."` 里带引号嵌套时 PowerShell 转义出错，
  报 `SyntaxError: '(' was never closed`（TRAE 之前的会话也踩过同一坑）。
- **根因**：PowerShell 对双引号/反斜杠的转义规则与 bash 不同，多层嵌套基本无解。
- **解法**：写临时脚本文件（如 `_verify_xxx.py`）放进项目内跑，跑完即删；
  项目内才能解析 `test_agent` 包依赖。
- **Java 线启示**：要跑 `mvn` 带 `-D` 参数的命令时，同样优先写成脚本/配置文件，
  不要在 PowerShell 内联拼长参数。

## 2. Docker Desktop 未启动，daemon 连不上

- **现象**：`docker images` 报 `failed to connect to the docker API at
  npipe:////pipe/dockerDesktopLinuxEngine`。
- **根因**：Docker Desktop 装了但没启动（本地开发机常态，重启后尤其）。
- **解法**：`Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"`
  后等约 30~60 秒再操作；代码侧（cli.py）已加友好提示。
- **Java 线启示**：构建 `mta-test:jdk17-maven` 前先 `docker info` 探活；
  CLI 的 daemon 探活检查对两条线通用。

## 3. put_archive 以 root 写入，非 root 用户写不了

- **现象**：快照复制进容器后，tester 用户跑 pytest 时写 `.pytest_cache`/
  覆盖率文件会权限失败（潜在，未触发即被拦截）。
- **根因**：docker SDK 的 `put_archive` 以 root 身份落文件，而镜像 USER 是 tester。
- **解法**：`copy_dir_into` 里复制完立刻以 root 执行
  `chown -R tester:tester /work`（见 sandbox/runner.py）。
- **Java 线启示**：Maven 要写 `~/.m2/repository`，同样需要归属正确；
  jdk17 镜像里 settings.xml 已预置在 tester 家目录，但仓库缓存目录首写时注意。

## 4. docker SDK 的 exec_run 没有原生超时

- **现象**：`container.exec_run(...)` 不支持 timeout 参数，命令挂起就永远挂起。
- **根因**：docker exec 是长连接流式 API，SDK 未封装超时中断。
- **解法**：线程 + `future.result(timeout)` 实现"超时返回"；
  超时后底层 exec 仍占用，**超时任务不得复用容器**，由队列层统一 cleanup
  （见 sandbox/runner.py 头部"诚实边界"注释）。
- **Java 线启示**：`mvn test` 首次要下依赖，耗时远超 pytest——超时阈值要放宽
  （建议 ≥900s），且"超时必回收容器"的纪律要执行得更严。

## 5. 同一任务多次运行，日志按序号交错显示

- **现象**：`cli.py log 6` 里出现两个 `[00]`、两个 `[03]`，内容还不一样。
- **根因**：任务 6 跑了两次 = 两条 `agent_sessions`，每条会话内 seq 从 0 重新计数，
  旧查询把两个会话的消息按 seq 合并排序了。
- **解法**：log 命令改为**按会话分组显示**（cli.py，2026-10-27 修）。
- **Java 线启示**：任何"按 seq 取增量"的查询（未来 L1 界面的 SSE 增量渲染同理）
  必须带 session_id 过滤，否则前端会出现同样的鬼影。

## 6. pip freeze 把整个 Anaconda 环境冻进 lock 文件

- **现象**：`pip freeze > requirements.lock.txt` 生成了几百个无关包
  （aext-assistant、croot 本地路径等）。
- **根因**：宿主是 Anaconda 全家桶，freeze 抓的是全环境不是项目依赖。
- **解法**：不用 freeze，直接在 requirements.txt 里 `==` 钉死实测通过的版本号。
- **Java 线启示**：Maven 没有这个问题（pom 天然显式依赖），但要防
  `-U`/SNAPSHOT/动态版本号——Java 镜像里同样把 Maven/JUnit 版本钉死。

## 7. 解析器依赖 pytest 输出格式（覆盖率/指纹），格式一变静默失效

- **现象**：覆盖率用 `TOTAL.*?\s(\d+)%` 正则提取、失败指纹依赖 `-rfE`
  短摘要的 `FAILED <nodeid> - <ErrType>` 格式。**这是整个系统最脆弱的一环**：
  pytest 升级改输出格式的话，覆盖率变 None、指纹永远为空 → 熔断静默失效。
- **根因**：解析器基于文本正则，没有格式契约。
- **解法**：双保险——① 镜像里钉死 pytest 9.1.1（工具链进镜像=格式锁死）；
  ② tests/test_repair.py 用录制的真实输出做固定 fixture，格式一变测试立刻红。
- **Java 线启示**：surefire 的 XML 报告 + JaCoCo 的 XML 报告是结构化格式，
  比 pytest 的文本输出稳——**Java 线优先解析 XML 而不是控制台文本**，
  这是 Java 线相对 Python 线可以做得更稳的地方。

## 8. LLM 返回的 JSON 解析失败

- **现象**：DeepSeek 偶尔在 JSON 外包 ```json 围栏、或输出多余说明文字，
  直接 `json.loads` 会炸。
- **根因**：LLM 输出本质不可靠，不能假设严格 JSON。
- **解法**：`_parse_cases` 用贪婪正则提取首个 `{...}` 块 + 只保留含
  `def test_` 的用例；解析失败返回 `{"generated": 0}` 诚实降级，
  不硬造用例（见 skills/gen_tests.py）。
- **Java 线启示**：同一套解析兜底逻辑可直接复用；生成 JUnit 用例时
  同样要校验 `@Test` 注解存在才落库。

## 9. 重跑脚本撞 SQLite UNIQUE 约束

- **现象**：冒烟脚本第二次跑报 `UNIQUE constraint failed: users.username`。
- **根因**：数据层是持久库，临时脚本用固定用户名重复插入。
- **解法**：`INSERT OR IGNORE` + SELECT 回读 id；所有辅助脚本按幂等写。
- **Java 线启示**：无专属启示，但"脚本必须可重跑"要成为纪律——
  Java 线的验收脚本同样按幂等设计。

## 10. analyze 语言统计的 dict 初始化 KeyError

- **现象**：`counts[lang] += n` 首个文件即抛 `KeyError: 'python'`。
- **根因**：dict 没初始化就累加（典型的可变默认值/初始化疏漏）。
- **解法**：`counts[lang] = counts.get(lang, 0) + n`。
- **Java 线启示**：无专属启示；但这类错误的教训是——**每个新节点先跑一次
  最小样本再进主图**，不要一次写完全链路才测。

## 11. LangGraph recursion_limit 嵌套传参不生效

- **现象**：`graph.invoke(init, config={"recursion_limit": 60})` 传了但怕不生效
  （复查时发现写法存疑）。
- **根因**：RunnableConfig 的 `recursion_limit` 必须是**顶层键**，
  嵌套在 `config` 键下会被忽略。
- **解法**：`graph.invoke(init, {"recursion_limit": 60})`（builder.py 已修）。
- **Java 线启示**：无专属启示；教训是"框架参数照官方签名传，不造嵌套"。

## 12. langchain / langgraph 版本迭代猛

- **现象**（预防性记录）：LangChain 系历史上 breaking changes 频繁，
  论文答辩时环境跑不起来是灾难性风险。
- **根因**：快迭代框架的通病。
- **解法**：requirements.txt `==` 钉死 + 镜像内工具链钉死 +
  tests/ 回归网（改版本前先跑测试）。
- **Java 线启示**：Java 生态版本纪律天然更好（BOM/锁定 plugin 版本），
  沿用 Maven 默认即可，但**不要**用 `latest` 版本号。

## 13. Docker 容器命名固定 → 同任务并发运行会撞名

- **现象**（潜在）：容器名固定为 `mta-run-<task_id>`，同一任务并发两次 run
  会争用同名容器。
- **根因**：单用户场景简化设计，名字可复用（幂等 ensure）。
- **解法**：当前单用户可接受；并发需求出现时改为 `mta-run-<task_id>-<session_id>`。
- **Java 线启示**：Maven 镜像更重，容器启动慢，**更**容易被并发触发的超时
  误伤——Java 线实现任务队列时直接带 session 后缀，一步到位。

## 14. 行为语义升级后，旧测试断言集体变红（2026-10-27 测试网首跑实录）

- **现象**：固化测试网第一次全量运行就 6 红：R-Loop 语义已被升级
  （疑似产品缺陷→立即冻结上报；覆盖率未知→不算达标；schema 13→14 张表），
  旧断言全部失效。
- **根因**：多会话并行开发，代码语义演进了，但缺少回归网兜底——这正是
  当初要建测试网的原因，首跑即验证了价值。
- **解法**：先读懂新语义确认是**有意设计**（不回炉迁就缺陷=防造假路径，
  更符合诚实纪律），再把测试更新到新语义并写明理由——测试不是将就代码，
  而是锁定"当前被认可的行为"。
- **Java 线启示**：语义级改动必须同步改测试并在 pitfalls 里留痕；
  Java 线开工时先跑一遍测试网确认基线，再动手。

---

## 附：待观察项（还不算坑，但 Java 线设计时要考虑）

1. **失败归因是启发式**：product_defect 只是"疑似"，靠 feedback_annotations
   人工标注闭环——Java 线保留同样设计，别试图做成全自动。
2. **data/workspaces 快照会积累**：加 `clean` 命令治理；Java 克隆更大，
   清理策略要更早考虑。
3. **环境变量 DEEPSEEK_API_KEY 只在当前终端有效**：新开终端忘配会静默降级为
   "跳过生成"——注意 run 结果里 generated=0 且 reason 带"未配置"时先查 Key。
