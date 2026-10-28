-- ============================================================================
-- 第5层·数据层 v1（SQLite 方言）
-- 毕设：《基于大模型 Agent 与容器技术的自动化软件测试方法研究》
--
-- 约定：
--   * 方言 SQLite（借用 Multi-tool Agent Runtime MVP 的 better-sqlite3 同步 API）
--   * 时间统一 TEXT ISO8601（UTC）；比值/覆盖率用 REAL（0~1）；耗时毫秒 INTEGER
--   * 枚举用 TEXT + CHECK，不建字典表（本科规模，简单优先）
--   * 外键运行时开启：PRAGMA foreign_keys = ON
--   * 覆盖学院要求：≥7 张表，且含 智能体 / 会话 / 工具 / 日志 / 评测 —— 本稿 17 张
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
                                     'done','failed','cancelled')),
  confirmed        INTEGER NOT NULL DEFAULT 0,        -- 人工确认门：0 = 禁止下发执行
  auto_prepare     INTEGER NOT NULL DEFAULT 0,        -- 沙箱自适应：1 = 开局自动拉取/构建执行镜像（模式③）
  confirmed_by     INTEGER REFERENCES users(id),
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
