# 代表 Skill 契约（T6 交付，2026-11-25）

> 目的：Skill 辅方向条款 (3)「每个 Skill 定义输入输出契约」与 (6)「现场演示调用过程」的固化样例。
> 主代表 **run_tests**（内置·高风险·显式确认门——黄金链"智能体→Skill→Docker→数据库"的实体环节）；
> 辅代表 **test.observe.read**（Agent 自主选择调用的能力型 Skill，2×2 实验中调用最多）。
> 现场演示器：`scripts/skill_demo.py`（零 LLM 零 Docker，`python -I scripts/skill_demo.py` 即可重演）。
> 生成方法说明：本契约由执行者对码 `builtin.py`/`capabilities.py`/`registry.py` 后填写；行号以 commit b2ae842 时点为准。

## 一、主代表 run_tests

| 项 | 内容 | 锚点 |
|---|---|---|
| 名称 | `run_tests` | `builtin.py:265` 注册 |
| 功能 | 容器内执行测试命令（pytest/jest/go test/phpunit…），stdout/stderr 分开返回供评估归因 | `builtin.py:178-205` |
| 输入 Schema | legacy 简写形态：`{"cmd": "list[str]"}`（必填 `cmd`=命令数组；缺键 legacy 兼容放行，**类型错必拒**） | `builtin.py:269` |
| 输出结构 | JSON 字符串 `{"exit": int, "stdout": str, "stderr": str}` | `builtin.py:204-205` |
| 权限与风险 | `risk_level="high"` + `requires_confirmation=True`（双重确认门：显式声明×0-B 风险分层——confirmed=0 fail-closed 拒绝） | `builtin.py:267-268`；`registry.py:169-177` |
| 超时 | `timeout_ms = DEFAULT_JOB_TIMEOUT_S×1000`；超时走注册表 0-C 真终止（coreutils timeout 包裹+cancel_event 通知+exit 124/137 映射） | `builtin.py:267`；`registry.py:194-200` |
| 异常降级 | 考场容器不可达→**诚实环境错误 JSON**（exit=-1+中文环境语义，非裸 404）；php 缺 vendor→前置探测诚实失败 | `builtin.py:180-186,208-216` |

**真实调用日志**（`python -I scripts/skill_demo.py` 产出，2026-11-25）：

```
#1 run_tests → denied (0ms)   err='人工确认门：任务未确认（confirmed=0），禁止下发'
#2 run_tests → ok (3ms)       output='{"exit": -1, "stderr": "环境错误：考场容器 mta-run-1 不可达…"}'
#3 run_tests → denied (0ms)   err="参数校验拒绝：键 'cmd' 类型应为 list[str]，实得 str"
```

### 四类测试矩阵（run_tests + 注册表共性）

| 类 | 测试（`pytest -k test_matrix_` 可复跑） | 断言要点 |
|---|---|---|
| 正常 | `test_matrix_normal_run_tests_container_ok` | 容器可达 → exit/stdout 结构化返回 |
| 异常 | `test_matrix_exception_run_tests_container_missing` | 容器缺失 → 诚实环境错误 JSON（非裸 404） |
| 权限 | `test_matrix_permission_confirmation_gate` | confirmed=0 → denied"人工确认门" |
| 边界 | `test_matrix_boundary_registry_timeout` | 超时闸 → timeout 审计行 + cancel_event 协议 |

既有补充覆盖：schema 双模式族 `tests/test_registry_gates.py`（缺键/未知键/类型/枚举/停用/简写兼容）；停用闸 `test_enabled_gate_denies`；确认门风险分层 `:120-124`。

## 二、辅代表 test.observe.read

| 项 | 内容 | 锚点 |
|---|---|---|
| 名称 | `test.observe.read` | `capabilities.py:375` schema 定义 |
| 功能 | Agent 决策循环的读证据能力：read（窗口化读文件）/ list（列目录）/ grep（上限 50 条）三模式 | `capabilities.py:113-114` handler |
| 输入 Schema | 严格 JSON Schema：`path`*、`mode`*(read/list/grep)、`pattern`、`offset`(≥1)、`limit`(1-200)，`additionalProperties: false`（未知键拒） | `capabilities.py:375-382` |
| 输出结构 | `CapResult{status, summary(窗口头), metrics, evidence[{kind:"file_read", path(证据文件指针), sha256}]}`——证据落盘不占上下文 | `capabilities.py` CapResult |
| 权限与风险 | R0（菜单可见性由语言+旗标闸控制）；策略闸白名单：路径逃逸/数据文件拒绝 | `tool_policy.py:57-61`；`capabilities.py` 白名单 |
| 超时 | `timeout_s=5`（经 Capability→Skill 包装 registry 超时） | `tool_loop.py:80-82` |
| 异常降级 | 文件缺失→failed 不虚构；读取受 200 行窗口/12000 字符观察包封顶 | `tool_loop.py:139` |

**真实调用日志**（同上 demo 产出）：

```
#4 test.observe.read → ok (6ms)    output='{"status":"ok","summary":"calc.py: 行 1-2（共 2 行，≤200 行窗口）",...}'
#5 test.observe.read → denied (0ms) err="参数校验拒绝：键 'limit'=999 高于上限 200"
#6 test.observe.read → denied (0ms) err='skill 已停用（enabled=0），拒绝执行'
```

### 四类测试矩阵（test.observe.read）

| 类 | 测试 | 断言要点 |
|---|---|---|
| 正常 | `test_matrix_normal_observe_read` | 读文件→窗口头+file_read 证据指针 |
| 异常 | `test_matrix_exception_observe_read_missing_file` | 文件不存在→failed 不虚构 |
| 权限 | `test_matrix_permission_disabled_skill` | enabled=0→停用闸拒绝 |
| 边界 | `test_matrix_boundary_observe_limit_schema` | limit=200 放行 / 201 拒（上限边界） |

既有补充覆盖：白名单逃逸拒绝 `test_read_whitelist_denies_data_and_escape`；grep 50 条封顶 `test_grep_capped_at_50`；语言/旗标菜单门 `test_menu_language_and_flag`。

## 三、验收口径与证据

- 矩阵每类 ≥1 条：`pytest -k test_matrix` → 8 passed（2026-11-25）。
- 演示器产出审计 ≥3 行：实际 6 行（六场景 6/6 命中+诚实降级 PASS）——`skill_invocations` 台账见上。
- 设计稿 §10.1 的"一条真实调用日志"：真机调用日志见循环点①真机格（agent_steps 台账 2-3 行/格，`data/reports/loop1_verify/cell_ledger_*.json` detail.steps）。
