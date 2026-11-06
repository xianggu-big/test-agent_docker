-- ============================================================================
-- 第5层·数据层 v1（SQLite 方言）
-- 毕设：《基于大模型 Agent 与容器技术的自动化软件测试方法研究》
--
-- 约定：
--   * 方言 SQLite（借用 Multi-tool Agent Runtime MVP 的 better-sqlite3 同步 API）
--   * 时间统一 TEXT ISO8601（UTC）；比值/覆盖率用 REAL（0~1）；耗时毫秒 INTEGER
--   * 枚举用 TEXT + CHECK，不建字典表（本科规模，简单优先）
--   * 外键运行时开启：PRAGMA foreign_keys = ON
--   * 覆盖学院要求：≥7 张表，且含 智能体 / 会话 / 工具 / 日志 / 评测 —— 本稿 24 张
--     （18 张 L1 全链路 + 19-22 张四级双轨 P1/P2：test_levels / interface_endpoints / call_edges / scenario_cases
--      + 第 23 张 review_decisions：AI 代审/人工审查决策台账，决策必须结构化+可解释
--      + 第 24 张 test_case_results：逐用例执行结果（实时执行看板数据源）)
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 1) 用户与权限（管理员 admin / 开发者 developer / 调用者 caller）
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'caller'
                CHECK (role IN ('admin','developer','caller')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2) 项目（一次测试的对象仓库；语言占比判定结果落在这里）
CREATE TABLE IF NOT EXISTS projects (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          INTEGER NOT NULL REFERENCES users(id),
  repo_url         TEXT NOT NULL,
  name             TEXT NOT NULL,
  primary_language TEXT,                 -- 主语言（占比最高且 ≥ 阈值 10%）
  language_ratios  TEXT,                 -- JSON：{"python":0.72,"java":0.11,"other":0.17}
  clone_status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (clone_status IN ('pending','ok','failed')),
  repo_key         TEXT,                 -- B2 项目归一键（normalize_repo_key：本地路径 resolve+normcase / URL 去 .git 小写 host）
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 3) 模块（多模块拆分与分级策略：<10% 语言 static_only，不拉执行镜像）
CREATE TABLE IF NOT EXISTS modules (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  path       TEXT NOT NULL,
  language   TEXT NOT NULL,
  loc        INTEGER NOT NULL DEFAULT 0,
  strategy   TEXT NOT NULL DEFAULT 'skip'
             CHECK (strategy IN ('full','static_only','skip')),
  UNIQUE (project_id, path)
);

-- 4) 测试任务（一次端到端测试 = 一条任务；状态机与界面进度条一一对应）
CREATE TABLE IF NOT EXISTS test_tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id       INTEGER NOT NULL REFERENCES projects(id),
  user_id          INTEGER NOT NULL REFERENCES users(id),
  requirement_text TEXT,                              -- 用户自然语言需求
  coverage_target  REAL,                              -- 准出标准：分析阶段冻结（如 0.70），decide 只认任务行的值
  status           TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','cloning','analyzing','provisioning',
                                     'generating','running','reporting',
                                     'ready_for_review',
                                     'done','failed','cancelled')),
  confirmed        INTEGER NOT NULL DEFAULT 0,        -- 确认门：0 = 禁止下发；两阶段任务确认=授权启动阶段一
  auto_prepare     INTEGER NOT NULL DEFAULT 0,        -- 沙箱自适应：1 = 开局自动拉取/构建执行镜像（模式③）
  two_stage        INTEGER NOT NULL DEFAULT 0,        -- 两阶段人机协作：1 = 确认后先跑准备阶段（克隆/摸底/考纲/出题），停在 ready_for_review 等审查
  ai_reviewer      TEXT NOT NULL DEFAULT 'off'
                   CHECK (ai_reviewer IN ('off','hybrid','auto')),  -- AI 代审三模式（docs/AI代审设计-20261031.md）
  confirmed_by     TEXT,                              -- 满足确认门的主体：用户 id 或 agent:reviewer-<session>（AI 代审，问责链）
  confirmed_at     TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 5) 智能体会话（Test Agent / Planner / Evaluator 各一轮编排 = 一条会话）
CREATE TABLE IF NOT EXISTS agent_sessions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES test_tasks(id),
  agent_role TEXT NOT NULL DEFAULT 'test_agent'
             CHECK (agent_role IN ('test_agent','planner','evaluator')),
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at   TEXT
);

-- 6) 会话消息（智能体多轮交互证据；答辩演示「多轮规划/判读」就查这张表）
CREATE TABLE IF NOT EXISTS agent_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES agent_sessions(id),
  seq        INTEGER NOT NULL,           -- 会话内序号，前端按 seq 增量渲染
  role       TEXT NOT NULL CHECK (role IN ('system','assistant','user','tool')),
  content    TEXT NOT NULL,
  tokens     INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (session_id, seq)
);

-- 7) 测试脚本插件与版本（固定框架 + 可插拔用例；版本不可变，确认后才能下发）
CREATE TABLE IF NOT EXISTS test_scripts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER NOT NULL REFERENCES test_tasks(id),
  module_id    INTEGER REFERENCES modules(id),
  framework    TEXT NOT NULL,             -- pytest / junit5
  version      INTEGER NOT NULL DEFAULT 1,
  filename     TEXT,                      -- 工作区文件名（test_gen_r1.py / test_gen_fb1.py…）：布考场按审查结论重建文件的键
  content      TEXT NOT NULL,             -- 脚本全文（含固定框架 + 已插入用例）
  content_hash TEXT NOT NULL,
  confirmed_by INTEGER REFERENCES users(id),
  confirmed_at TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (task_id, module_id, version)
);

-- 8) 用例库（内置 builtin / AI 生成 ai / 外部导入 imported 三来源统一入库）
CREATE TABLE IF NOT EXISTS test_cases (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  script_id      INTEGER NOT NULL REFERENCES test_scripts(id),
  source         TEXT NOT NULL CHECK (source IN ('builtin','ai','imported')),
  name           TEXT NOT NULL,
  method         TEXT,                        -- 测试方法标签：等价类/边界值/判定表/覆盖/错误推测
  content        TEXT NOT NULL,
  priority       TEXT,                        -- 六要素：重要级别（对应用户需求优先级）
  input          TEXT,                        -- 六要素：测试输入条件
  steps          TEXT,                        -- 六要素：操作步骤
  expected       TEXT,                        -- 六要素：预期结果（断言先于执行）
  expected_basis TEXT,                        -- 预期值出处：需求明示/docstring/源码推导/常识（§7 归因证据）
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 9) Skill 注册表（Schema、权限、超时 —— 对齐借用框架 ToolRegistry 的约定）
CREATE TABLE IF NOT EXISTS skills (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,      -- clone_repo / detect_lang / start_container /
                                          -- gen_tests / run_tests / collect_report / ...
  version      TEXT NOT NULL DEFAULT '1.0.0',
  input_schema TEXT NOT NULL,             -- JSON Schema
  risk_level   TEXT NOT NULL DEFAULT 'low'
               CHECK (risk_level IN ('low','medium','high')),
  timeout_ms   INTEGER NOT NULL DEFAULT 60000,
  enabled      INTEGER NOT NULL DEFAULT 1
);

-- 10) Skill 调用日志（工具维度审计 + 成本核算；论文「工具调用成功率」指标数据源）
CREATE TABLE IF NOT EXISTS skill_invocations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES test_tasks(id),
  skill_id    INTEGER NOT NULL REFERENCES skills(id),
  args_json   TEXT,
  status      TEXT NOT NULL DEFAULT 'running'
              CHECK (status IN ('running','ok','failed','timeout','denied')),
  duration_ms INTEGER,
  tokens      INTEGER,
  error       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 11) 容器任务队列（L4 调度器工作队列 + 资源限额台账）
CREATE TABLE IF NOT EXISTS container_jobs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER NOT NULL REFERENCES test_tasks(id),
  module_id    INTEGER REFERENCES modules(id),
  image        TEXT NOT NULL,             -- mta-test:py312 / mta-test:jdk17-maven
  priority     INTEGER NOT NULL DEFAULT 5,-- 小值先跑（改动模块/主路径优先）
  status       TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','starting','running','done','failed',
                                 'timeout','cancelled')),
  cpu_limit    REAL NOT NULL DEFAULT 1.0,
  mem_limit_mb INTEGER NOT NULL DEFAULT 1024,
  timeout_at   TEXT,
  container_id TEXT,
  started_at   TEXT,
  finished_at  TEXT
);

-- 12) 测试报告与指标（论文核心实验表：通过率 / 覆盖率 / 缺陷 / 耗时）
CREATE TABLE IF NOT EXISTS test_reports (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         INTEGER NOT NULL REFERENCES test_tasks(id),
  module_id       INTEGER REFERENCES modules(id),
  passed          INTEGER NOT NULL DEFAULT 0,
  failed          INTEGER NOT NULL DEFAULT 0,
  skipped         INTEGER NOT NULL DEFAULT 0,
  coverage_line   REAL,                  -- 0~1，行覆盖率
  coverage_branch REAL,                  -- 0~1，分支覆盖率
  defects         TEXT,                  -- JSON：疑似缺陷清单（文件/行/描述/置信度/分类）
  per_file        TEXT,                  -- JSON：逐文件覆盖率与未覆盖行（B2：覆盖率趋势图与缺口回灌数据源）
  narrative       TEXT,                  -- gen_report 叙述正文（markdown；数字只能来自指标包，E5 审查对象）
  review          TEXT,                  -- JSON：报告二次审查结论 {status, reasons, reviewer}（§3.4 双闸）
  analysis        TEXT,                  -- JSON：AI 缺陷分析与修复建议 {summary, defects[]}（报告概览失败框，按需生成缓存；root_cause/fix 为推测）
  duration_ms     INTEGER,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 13) 误报/漏报人工标注（正确率闭环：标注回流 → 提示词与规则改进，可写对比实验）
CREATE TABLE IF NOT EXISTS feedback_annotations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id    INTEGER NOT NULL REFERENCES test_reports(id),
  case_id      INTEGER REFERENCES test_cases(id),
  label        TEXT NOT NULL
               CHECK (label IN ('false_positive','missed','valid','env_issue')),
  note         TEXT,
  annotator_id INTEGER NOT NULL REFERENCES users(id),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 14) 测试条件（RTM 需求追踪矩阵的条件层：需求 → 测试条件 → 用例 可追溯，防需求变更失控）
CREATE TABLE IF NOT EXISTS test_conditions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         INTEGER NOT NULL REFERENCES test_tasks(id),
  requirement_ref TEXT,                    -- 需求原文关键句（变更影响分析的反查键）
  condition       TEXT NOT NULL,           -- 可验证的测试条件陈述
  priority        TEXT,                    -- 高/中/低（对应用户需求优先级）
  method_hint     TEXT,                    -- 建议用例设计方法（五方法之一）
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 15) RTM 映射（map_requirements 产物：需求 → 测试条件 → 用例 → 结果 的矩阵快照，每次评估覆盖）
CREATE TABLE IF NOT EXISTS rtm_maps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES test_tasks(id),
  report_id   INTEGER REFERENCES test_reports(id),
  source      TEXT NOT NULL DEFAULT 'heuristic'
              CHECK (source IN ('llm','heuristic')),
  matrix      TEXT NOT NULL,             -- JSON：[{condition_id, condition, priority, cases, status}]
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 16) 评估轮次台账（P1-③ 根修：R-Loop 每轮评估的结构化落库。此前每轮 passed/failed/
--     覆盖率/判定只存 agent_messages 叙述消息，collect_metrics 靠正则解析散文取轨迹，
--     措辞一变轨迹即静默清空；本表为轨迹唯一真源，叙述消息仅留人读）
CREATE TABLE IF NOT EXISTS evaluation_rounds (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       INTEGER NOT NULL REFERENCES test_tasks(id),
  level         TEXT NOT NULL DEFAULT 'L1', -- 级别维度（四级双轨）：L1 存量行为不变（默认 L1），L2 评估写 'L2'
  round_no      INTEGER NOT NULL,         -- 本轮编号（重跑从 1 重新计，delete-first 整体替换）
  passed        INTEGER NOT NULL DEFAULT 0,
  failed        INTEGER NOT NULL DEFAULT 0,
  skipped       INTEGER NOT NULL DEFAULT 0,
  coverage_line REAL,                     -- 0~1，行覆盖率（无覆盖率数据时 NULL）
  verdict       TEXT,                     -- R-Loop 判定：pass / repair_gen / repair_env / stop
  failure_kind  TEXT,                     -- 归因分类：use_case / env / product / none
  stop_reason   TEXT,
  fingerprint   TEXT,                     -- 失败指纹（熔断判定的输入，留档可复现）
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 17) 复杂度热点快照（W4：analyze 阶段 radon 确定性扫描落库，报告指标"复杂度热点
--     未覆盖"由本表 × test_reports.per_file 在读取时确定性求值——覆盖状态随每份
--     报告的覆盖数据而变，属派生量不回写本表；任务重跑 delete-first 整体替换）
CREATE TABLE IF NOT EXISTS complexity_hotspots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  file       TEXT NOT NULL,               -- workspace 相对路径（POSIX 风格，与 per_file 同口径）
  name       TEXT NOT NULL,               -- 函数/方法名
  complexity INTEGER NOT NULL,            -- V(G) 圈复杂度（radon）
  rank       TEXT NOT NULL,               -- radon 六级制 A-F（入库均为热点 rank≥C）
  lineno     INTEGER NOT NULL,            -- 函数起始行（与 per_file.missing 交叉判覆盖）
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, file, name)
);

-- 18) LLM 调用成本台账（P1-E：每次真实 LLM 调用一行，token 用量落库。覆盖全部
--     7 个调用点——节点直调（plan）与 skill func 内调用（gen_tests 主生成/方案C
--     补生成/rewrite 重写/RTM 映射/报告叙述/报告审查）；论文成本-质量曲线、
--     "每检出 1 个缺陷的 token 成本"的数据源。usage 缺失（fake-LLM/降级链路）
--     不落行——诚实不记 0；追加行不 delete-first：跨轮次/回炉的成本累积即口径）
CREATE TABLE IF NOT EXISTS llm_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id           INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  phase             TEXT NOT NULL,           -- plan / gen_tests / refill / rewrite / map_rtm / gen_report / review_report
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  total_tokens      INTEGER NOT NULL,        -- = prompt + completion（langchain usage_metadata 原值，缺失时按和补）
  model             TEXT,                    -- config.DEEPSEEK_MODEL 快照（成本换算需按模型计价）
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 19) 测试级别台账（四级双轨 D9：level_supervisor 串行接力的账本——每任务×级别一行，
--     上一级 leg 的 id 经 trace_from 串成追溯链头；summary 由确定性代码写本级指标摘要）
CREATE TABLE IF NOT EXISTS test_levels (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  level       TEXT NOT NULL CHECK (level IN ('L1','L2','L3','L4')),
  strategy    TEXT NOT NULL DEFAULT 'full',   -- full / semi_auto / assist / degraded（D4 降级时如实标）
  status      TEXT NOT NULL DEFAULT 'queued'
              CHECK (status IN ('queued','running','done','failed','skipped','cancelled')),
  verdict     TEXT,                           -- 本级终判（pass/stop/...，不跨级改判）
  stop_reason TEXT,
  trace_from  INTEGER REFERENCES test_levels(id),  -- 接力来源（上一级 leg id），跨级 RTM 溯源链头
  summary     TEXT,                           -- JSON：本级指标摘要（端点覆盖/契约违背/场景通过率…）
  started_at  TEXT,
  finished_at TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (task_id, level)
);

-- 20) 接口端点清单（L2 黑盒轨输入：D4 降级链产物——①自带 OpenAPI spec 解析（P1）
--     ②框架路由 AST 提取（P2）③诚实降级不入行；source 记录提取路径供归因与消融）
CREATE TABLE IF NOT EXISTS interface_endpoints (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  method          TEXT NOT NULL,              -- GET/POST/PUT/DELETE/PATCH
  path            TEXT NOT NULL,              -- /orders/{id}
  operation_id    TEXT,                       -- spec 的 operationId（FastAPI 默认值可反查 handler 函数名）
  params          TEXT,                       -- JSON：path/query 参数表（含 schema）
  request_schema  TEXT,                       -- JSON：requestBody schema（$ref 已确定性展开）
  response_schema TEXT,                       -- JSON：首个 2xx 响应 schema（$ref 已展开）
  source          TEXT NOT NULL DEFAULT 'spec'
                  CHECK (source IN ('spec','ast','llm')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (task_id, method, path)
);

-- 21) 调用边快照（L2 白盒轨：AST 确定性调用图——端点 handler 为根（caller 记 route:METHOD /path），
--     callee 为被调函数，depth 为距 handler 的调用深度；接口顺序约束场景的生成依据）
CREATE TABLE IF NOT EXISTS call_edges (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  caller     TEXT NOT NULL,                   -- 'route:POST /orders' 或 'module.func'
  callee     TEXT NOT NULL,
  depth      INTEGER NOT NULL DEFAULT 1,      -- 距 handler 深度（route 行 depth=1）
  file       TEXT,                           -- 被调函数定义所在文件（workspace 相对路径）
  lineno     INTEGER,                        -- 被调函数定义起始行（跨级溯源代码位置）
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_tasks_status     ON test_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project    ON test_tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_msgs_session     ON agent_messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_invocations_task ON skill_invocations(task_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status      ON container_jobs(status, priority);
CREATE INDEX IF NOT EXISTS idx_reports_task     ON test_reports(task_id);
CREATE INDEX IF NOT EXISTS idx_feedback_report  ON feedback_annotations(report_id);
CREATE INDEX IF NOT EXISTS idx_conditions_task  ON test_conditions(task_id);
CREATE INDEX IF NOT EXISTS idx_rtm_task         ON rtm_maps(task_id);
CREATE INDEX IF NOT EXISTS idx_eval_rounds_task ON evaluation_rounds(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task   ON llm_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_levels_task      ON test_levels(task_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_task   ON interface_endpoints(task_id);
CREATE INDEX IF NOT EXISTS idx_call_edges_task  ON call_edges(task_id);

-- 22) L3/L4 业务流场景（半自动：冒烟自动判定 + 失败人工介入 D5；L4 预留 G/W/T）
CREATE TABLE IF NOT EXISTS scenario_cases (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         INTEGER NOT NULL REFERENCES test_tasks(id) ON DELETE CASCADE,
  level           TEXT NOT NULL DEFAULT 'L3' CHECK (level IN ('L3','L4')),
  title           TEXT NOT NULL,
  requirement_ref TEXT,
  steps           TEXT NOT NULL,           -- JSON: [{method,path,body?,expect_status?},...]
  expected        TEXT,                    -- 场景预期（业务叙述）
  gwt             TEXT,                    -- JSON: {given,when,then}（L4 预留）
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','auto_pass','pending_confirm',
                                    'confirmed_pass','confirmed_defect','confirmed_scenario')),
  last_result     TEXT,                    -- JSON: 最近一次冒烟执行结果摘要
  actual_note     TEXT,                    -- 人工确认记录的实际表现
  confirmed_by    INTEGER REFERENCES users(id),
  confirmed_at    TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scenarios_task ON scenario_cases(task_id, level);

-- 23) AI 代审/人工审查决策台账（docs/AI代审设计-20261031.md：决策必须结构化+可解释，
--     reasoning 必须引用证据——条件编号/断言类型/覆盖缺口，静态闸校验防 vibes 审查）
CREATE TABLE IF NOT EXISTS review_decisions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        INTEGER NOT NULL REFERENCES test_tasks(id),
  decision       TEXT NOT NULL
                 CHECK (decision IN ('approve','reject','regenerate','insert')),
  target         TEXT,                   -- 决策对象：任务级准入='task' / 用例名 / '用例集' / 缺失行为描述
  reasoning      TEXT NOT NULL,          -- 审查理由（像人写审查意见：为什么通过/删除/再生成/插入）
  evidence       TEXT,                   -- JSON：{condition_ids:[...], risk_points/notes}
  reviewer_role  TEXT NOT NULL DEFAULT 'ai_proxy'
                 CHECK (reviewer_role IN ('ai_proxy','human')),
  round          INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_decisions_task ON review_decisions(task_id);

-- 24) 逐用例执行结果（任务C/2026-11-01：实时执行看板——每轮跑完逐用例落库，
--     前端轮询展示；pytest --junitxml / jest --json 确定性解析，零 LLM）
CREATE TABLE IF NOT EXISTS test_case_results (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER NOT NULL REFERENCES test_tasks(id),
  round_no     INTEGER NOT NULL DEFAULT 1,
  level        TEXT NOT NULL DEFAULT 'L1',
  nodeid       TEXT NOT NULL,             -- pytest path::name / jest fullName
  name         TEXT NOT NULL,             -- 剥路径的用例名（与 test_cases.name 对齐判 origin）
  status       TEXT NOT NULL
               CHECK (status IN ('passed','failed','error','skipped')),
  duration_ms  INTEGER,
  message      TEXT,                      -- 失败/错误消息摘要（截断存档）
  origin       TEXT NOT NULL DEFAULT 'generated'
               CHECK (origin IN ('generated','repo')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_case_results_task ON test_case_results(task_id, round_no);

-- 25) 执行队列（B1/2026-11-02：container_jobs 台账非真队列——并发下发多任务同时跑，
--     容器互挤 + SQLite 写竞争。本表=真队列：单 worker 串行消费，重启可恢复）
CREATE TABLE IF NOT EXISTS exec_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES test_tasks(id),
  kind        TEXT NOT NULL CHECK (kind IN ('prep','exec','auto')),
  status      TEXT NOT NULL DEFAULT 'queued'
              CHECK (status IN ('queued','running','done','failed','cancelled')),
  enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at  TEXT,
  finished_at TEXT,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_queue_status ON exec_queue(status, id);
CREATE INDEX IF NOT EXISTS idx_exec_queue_task ON exec_queue(task_id);

-- 26) 环境 recipe 台账（任务F/2026-11-02）：探测→配方→校验→构建→自检全链留痕，
--     供应链可审计（同 review_decisions 纪律）；source=deterministic|llm，stage 记录死在哪一步
CREATE TABLE IF NOT EXISTS image_recipes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES test_tasks(id),
  source      TEXT NOT NULL DEFAULT 'deterministic'
              CHECK (source IN ('deterministic','llm','user')),
  recipe      TEXT NOT NULL,          -- JSON 原文（含 evidence.detected_from）
  image_tag   TEXT,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','built','failed','rejected')),
  stage       TEXT,                   -- validate/dockerfile/build/health/done
  error       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_image_recipes_task ON image_recipes(task_id);

-- 24) 登录/使用记录（个人信息页：登录、退出、注册、失败尝试；任务 4）
CREATE TABLE IF NOT EXISTS login_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER REFERENCES users(id),
  username   TEXT NOT NULL,            -- 冗余存名（用户被删仍可读）
  event      TEXT NOT NULL CHECK (event IN ('login','logout','register','failed')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_events_user ON login_events(user_id, created_at);

-- 28) 应用运行时日志分档（黑盒能力补齐阶段1：被测服务进程 stdout/stderr 独立采集——
--     容器内重定向 /work/logs/app.log，docker logs 兜底；确定性正则按语言解析错误/警告行，
--     LLM 只在 log_analyze 做摘要且必须引用 line_no，日志原文可追溯）
CREATE TABLE IF NOT EXISTS app_logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES test_tasks(id),
  round_no   INTEGER NOT NULL DEFAULT 1,   -- 按轮分档；服务启动失败发生在用例执行前，记 round 1
  level      TEXT NOT NULL CHECK (level IN ('error','warn')),
  kind       TEXT NOT NULL,                -- python_traceback/python_exception/java_exception/java_stack/
                                           -- java_caused_by/go_panic/go_goroutine/node_rejection/node_error/
                                           -- error_log/warn_log
  line_no    INTEGER NOT NULL,             -- 日志文件内 1-based 行号（证据可追溯）
  message    TEXT NOT NULL,                -- 行内容裁剪（≤300 字符）
  raw_line   TEXT NOT NULL,                -- 原始行（逐字）
  source     TEXT NOT NULL CHECK (source IN ('file','docker_logs')),
  log_path   TEXT,                         -- 宿主侧快照路径 data/workspaces/<tid>/logs/app_r<N>.log
  log_lines  INTEGER NOT NULL DEFAULT 0,   -- 本轮采集时日志总行数（log_analyze 引用闸的合法域上界）
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_app_logs_task_round ON app_logs(task_id, round_no);
