# 《Agent Loop 实施附录——接口速查 / DDL / 提示词草稿 / Schema / 预扫 / 真值》

> 服务对象：`docs/设计方案-AgentLoop文件级实施设计-20261120.md`（五包设计，D1-D8 已拍板）
> 性质：实施前的最后一份零 API 准备文档——把实施第一小时就要查的东西全部预置好，避免开工时反复重读文件。
> 对码时间：2026-11-20 深夜。所有签名/行号/清单均为当时代码实测。
> 铁律重申：本附录不含可执行代码的"实施授权"——开工仍按执行序逐包先红后绿。

---

## 件 1：复用接口速查表（capability handler 的全部依赖签名）

### 1.1 现有函数精确签名（实测行号，实施时直接 import）

| 函数 | 签名 | capability 用途 |
|---|---|---|
| `stack_detect.detect_stack` | `skills/stack_detect.py:53` `detect_stack(ws: str \| Path) -> dict` | `test.observe.env`：语言/包管理器/版本探测（零容器） |
| `attribution.canary_cmd` | `skills/attribution.py:32` `canary_cmd(lang: str) -> list[str]` | `test.observe.env` 容器侧健康探针 |
| `case_results.parse_junit_xml` | `skills/case_results.py:18` `parse_junit_xml(text: str) -> list[dict]` | `test.run.unit` 结果解析（pytest 轨） |
| `case_results.parse_jest_results` | `skills/case_results.py:67` `parse_jest_results(stdout: str) -> list[dict]` | 同上（jest 轨） |
| `case_results.store_case_results` | `:105` `store_case_results(conn, task_id, round_no, rows, ...)` | run.unit 落台账（round_no 传负数=agent 步标记，**见 1.3 口径**） |
| `project_index.coverage_check` | `skills/project_index.py:406` `coverage_check(state: dict, per_file: list[dict], coverage_line: float \| None = None) -> dict` | `test.observe.coverage`：返回 `ok/zero_coverage/not_routed/scope_empty` 判读 |
| `attribution.run_diff_gate` | `skills/attribution.py:110` `run_diff_gate(exec_fn, lang, failed_files, v_ref, timeout_s=600, bug_stderr="") -> dict` | `test.diff.replay`：exec_fn 注入 `runner.exec_in`；no_git/no_ref 如实降级 dict |
| `attribution.outcome_passed` | `:171` `outcome_passed(lang, exit_code, stdout) -> bool` | run.unit 判通过口径（语言差异封装在此） |
| `gen_tests.generate_tests` | `skills/gen_tests.py:1727` `generate_tests(api_key: str, args: dict, task_id: int, runner) -> str`（返回 JSON 字符串） | `test.gen.cases`：args 带 `feedback`/`fb_no`/`version` 即增补形态（见 `builder.py:558-569` 现成调用样例） |
| `db_assert.db_assert` | `skills/db_assert.py:105` `db_assert(api_key: str, args: dict, task_id: int, runner) -> str` | `test.assert.db` |
| `ui_assert.ui_assert` | `skills/ui_assert.py:647` `ui_assert(api_key: str, args: dict, task_id: int, runner) -> str` | `test.assert.ui` |
| `grpc_assert.grpc_assert` | `skills/grpc_assert.py:98` `grpc_assert(api_key: str, args: dict, task_id: int, runner) -> str` | `test.assert.grpc` |
| builtin 包装（现成） | `skills/builtin.py:242/247/252` `db_assert/ui_assert/grpc_assert(task_id, args) -> str` | **直接复用这三行包装**，不用重包——build_skills(runner) 已闭包好 runner |
| `routes.route_plan` | `skills/routes.py:233` `route_plan(requirement, features, language, conn, task_id, flags) -> dict` | strategy 输入：flags 传全开取理论可选路线全集（`conn=None, task_id=None` 时零落库纯函数） |
| `project_index.dependency_closure / match_requirement / scope_prefixes_for` | `nodes.py:1007-1021` 现成调用形态 | strategy 输入：索引命中+闭包 |
| `snapshot 路线` | `routes.py:184` `snapshot_golden` 路线 → 执行函数在 static/snapshot route 模块 | `test.snapshot`：**旗标 `SNAPSHOT_ROUTE_ENABLED` 联动**（关=能力不入菜单） |

### 1.2 capability handler 统一形态（写法定死，避免实施时各写各的）

```python
# capabilities.py 内每个 handler 的统一签名（CapResult 见设计稿 §3.1）
def _cap_read(ctx: TaskCtx, args: dict) -> CapResult: ...
# TaskCtx = dataclass(task_id, workspace, container_name, primary_language, image,
#                      state_snapshot: dict, conn, runner, artifacts_dir: Path)
# 约定三条：
# ① handler 永不写 verdict/终态；② metrics 只能来自解析器函数（1.1 表）；
# ③ evidence 文件必须落在 ctx.artifacts_dir（data/artifacts/task-{tid}/cap-step{n}/）
```

### 1.3 记账口径（防双轨）

- LLM 调用：`make_chat(phase="agent_loop", task_id=…)` → `llm_calls` 自动落（phase 原始值）。
- 能力执行：**复用 `skill_invocations` 审计台账**——`SkillRegistry.invoke(task_id, skill, args)` 已有 `_begin/_log` 完整闭环（`registry.py:102-131`）。做法：每个 Capability 注册时**同时**注册一个同名 Skill（包装 handler），executor 走 `invoke()` =零第二套审计。
- `store_case_results` 的 `round_no`：agent 步产生的执行**不混进正常轮次**——传 `round_no=10000+step`（哨兵段，报告页过滤掉），避免污染 R-Loop 轮次轨迹语义。

---

## 件 2：新表 DDL 与守卫同步清单（包 2 三张表）

### 2.1 DDL 追加位置

真源=`docs/layer5-data-schema.sql`（`config.py:13` `SCHEMA_PATH`，当前 **34 张表**）。追加三张（编号顺延，仍写"IF NOT EXISTS"）：

```sql
-- 第 40-42 表（Agent Loop 包 2，2026-11-20 设计稿 §4.3）
CREATE TABLE IF NOT EXISTS tool_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id TEXT NOT NULL UNIQUE,          -- 目录条目稳定 id（如 tool.coverage.pcov）
    version TEXT NOT NULL,                    -- 固定版本（禁 latest）
    digest TEXT NOT NULL,                     -- sha256 摘要（目录文件为真源，表为可查询副本）
    source_type TEXT NOT NULL,                -- pypi/npm/local-script
    source_url TEXT,
    license TEXT,
    entrypoint TEXT,
    permissions_json TEXT NOT NULL,           -- {"fs":[...],"network":"none","secrets":[]}
    fixtures_json TEXT,                       -- happy_path/known_bad/timeout 三类金样路径
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','verified','active','deprecated','revoked')),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_installations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    catalog_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'installed' CHECK (status IN ('installed','verified','failed','rolled_back')),
    installed_at TEXT,
    verify_report TEXT                        -- 三类 fixture 契约验证结果 JSON
);
CREATE TABLE IF NOT EXISTS plugin_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    capability_needed TEXT NOT NULL,          -- 模型描述的能力缺口
    evidence TEXT,                            -- 引用的证据/摘要
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by TEXT,                          -- 人工审批者（Agent 永不能写 approved）
    decided_at TEXT
);
```

### 2.2 守卫同步三处（漏一处=守卫红，项目实锤纪律）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `docs/layer5-data-schema.sql` | 追加上述三表（34→37 表） |
| 2 | `scripts/module_check.py:159` `expected_tables` 集合 | 追加 `"tool_catalog", "tool_installations", "plugin_proposals"`，注释"37 表契约"（现注释写 34 表，`:168-170` 的标签字符串同步改） |
| 3 | `tests/test_db.py:2` `assert db.init_db(...) == 34` | 两处 34→37（docstring 的"表数量当前为 27"注释也一并更新为 37，防下一个读档的人困惑） |

`_MIGRATIONS`（`db.py:114`）**不需要动**——三张全是新表走 CREATE IF NOT EXISTS，老库下次 init 即补建（init_db 幂等逻辑 `db.py:208-216`）。只有给**已有表**加列才走 `_MIGRATIONS`。

---

## 件 3：提示词 v0 草稿（四个，对齐 prompt_library 现有风格）

风格锚点：常量段用大写蛇形（`ENV_CONTRACT_SECTION`，`prompt_library.py:301`），条件注入函数用 `_xxx_section` 小写（`:459 _endpoint_gap_section`），空值=空串输出逐字节不变。

### 3.1 `AGENT_STRATEGY_SYSTEM`（strategy 节点）

```
你是测试策略规划助手。你只能输出一个 JSON 对象，不输出任何其他文本。

## 你的任务
根据任务上下文和能力菜单，产出一份测试策略：先测什么、用什么能力、预算怎么分。

## 能力菜单（当前任务可用）
{capability_menu}          ← 每行一条：id | 风险级 | 一句话说明

## 硬约束（违反即被平台拒绝，不要尝试）
1. 只能使用菜单列出的能力 id，虚构 id 会被拒绝并浪费步数。
2. 观察类（R0）优先于执行类（R1）：先看清环境和范围，再动手生成。
3. 你的预算：最多 {max_steps} 步 / {max_tokens} token。策略不执行测试，只规划。

## 输出 JSON schema
{"priorities": [{"order": 1, "capability_id": "...", "why": "一句话理由",
                 "args_hint": {"...": "..."}}],
 "evidence_plan": ["每个优先项期望拿到什么证据，如 coverage_json"],
 "fallback": "预算耗尽或能力不足时的降级路线（一句话）"}
```

### 3.2 `AGENT_DIAGNOSE_SYSTEM`（diagnose 节点，分类学对齐——arXiv:2611.13395）

```
你是测试失败诊断助手。你只能输出一个 JSON 对象。

## 你的任务
基于给出的执行证据，把上一轮失败归入四类之一，并给出下一步动作建议。

## 分类定义（与证据严格对应，不许凭感觉）
- scaffold_error：导入路径/脚手架/文件布局错（如 Cannot find module、收集失败）
- env_error：依赖缺失/服务未起/连接拒绝（如 ECONNREFUSED、ModuleNotFoundError 第三方包）
- product_defect：被测代码行为与需求预期不符，且用例有真实依据（expected_basis）
- expectation_dispute：用例预期本身存疑（迁就/自证/依据不足）

## 纪律
1. 每个结论必须引用至少一条证据（引用 evidence 列表的序号）。
2. 证据不足时 confidence 用 "low"，宁可不确定也不硬猜。
3. 你不能修改任何文件，不能重跑测试，只能读证据。

## 输出 JSON schema
{"kind": "scaffold_error|env_error|product_defect|expectation_dispute",
 "confidence": "high|medium|low",
 "evidence_refs": [1, 3],
 "reason": "一句话归因",
 "suggested_action": "generate|scaffold_fix|env_fix|report_human 之一"}
```

### 3.3 `AGENT_DECIDE_SYSTEM`（tool_loop 每步决策，call/finish 协议）

```
你是测试 Agent 的决策器。你只能输出一个 JSON 对象，两种形态之一。

## 形态一：调用一个能力
{"action": "call", "capability": "<菜单中的 id>", "args": {…},
 "reason": "一句话：为什么现在调用它"}

## 形态二：结束循环
{"action": "finish", "summary": "≤200 字总结已获得的证据与剩余缺口"}

## 硬约束
1. capability 必须来自菜单，args 必须符合该能力的参数说明。
2. 同一个动作（能力+参数完全相同）重复出现会被平台停止。
3. 连续被拒绝说明你越出了权限，换目标而不是换姿势重试。
```

### 3.4 注入段（进 plan / generate 提示词，空=空串纪律）

```python
def _agent_strategy_section(strategy: dict | None) -> str:
    """strategy 产物注入 plan 提示词（设计稿 §3.7）。无策略=空串，输出逐字节不变
    （_endpoint_gap_section 同款纪律，prompt_library.py:459）。"""
    if not strategy:
        return ""
    lines = ["## 测试策略建议（Agent 策略节点产出，供参考——测试条件仍必须锚真值清单）"]
    for p in (strategy.get("priorities") or [])[:5]:
        lines.append(f"- {p.get('order')}. {p.get('capability_id')}：{p.get('why')}")
    fb = strategy.get("fallback")
    if fb:
        lines.append(f"- 降级路线：{fb}")
    return "\n".join(lines) + "\n"

def _agent_diagnosis_section(diagnosis: dict | None) -> str:
    """diagnose 产物注入 generate 提示词（"## 上一轮失败诊断"）。空=空串。"""
    if not diagnosis:
        return ""
    kind_map = {"scaffold_error": "脚手架/布局问题", "env_error": "环境问题",
                "product_defect": "疑似产品缺陷（证人纪律照常）",
                "expectation_dispute": "用例预期存疑"}
    return (f"## 上一轮失败诊断\n上一轮失败最可能原因：{kind_map.get(diagnosis.get('kind'), diagnosis.get('kind'))}"
            f"（置信 {diagnosis.get('confidence')}，依据：{diagnosis.get('reason')}）。\n"
            "本轮生成请针对性规避该类问题；若诊断为疑似产品缺陷，用例照常如实断言预期，"
            "不得迁就当前行为。\n")
```

**纪律锚**：诊断注入是"信息提供"不是"行为指令"（防御矩阵第一原则）——`product_defect` 分支必须显式写"不得迁就"，防诊断反而诱导迁就。

---

## 件 4：十二项 capability 的 inputs_schema 实文

复用 0-B 自写校验器方言（type/required/enum/min/max/additionalProperties:false）。逐项：

```python
SCHEMAS = {
 "test.observe.read": {"type": "object",
   "properties": {"path": {"type": "string"}, "mode": {"enum": ["read", "list", "grep"]},
                  "pattern": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
   "required": ["path", "mode"], "additionalProperties": False},
   # D7 调研修正 2：read 输出截窗——mode=read 单文件 ≤200 行窗口；grep 结果 ≤50 条封顶
 "test.observe.env": {"type": "object", "properties": {"probe": {"enum": ["stack", "canary", "ports"]}},
   "required": ["probe"], "additionalProperties": False},
 "test.run.unit": {"type": "object",
   "properties": {"targets": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
                  "timeout_s": {"type": "integer", "minimum": 10, "maximum": 600}},
   "required": ["targets"], "additionalProperties": False},
 "test.observe.coverage": {"type": "object", "properties": {}, "additionalProperties": False},
   # 纯读 state.per_file + coverage_check，无参数
 "test.observe.logs": {"type": "object",
   "properties": {"tail_n": {"type": "integer", "minimum": 1, "maximum": 50}},
   "additionalProperties": False},
 "test.gen.cases": {"type": "object",
   "properties": {"feedback": {"type": "string", "maxLength": 1500}, "targets": {"type": "array", "items": {"type": "string"}}},
   "required": ["feedback"], "additionalProperties": False},
   # 走 run_feedback_round 同款三道闸+预算（builder.py:508 现成管线）
 "test.edit.scaffold": {"type": "object",
   "properties": {"path": {"type": "string"}, "new_content": {"type": "string", "maxLength": 20000},
                  "reason": {"type": "string"}},
   "required": ["path", "new_content", "reason"], "additionalProperties": False},
   # 写前必过 _classify_edit_diff 四分类闸；断言类拒绝→oracle_change_request
 "test.assert.db":  {"type": "object", "properties": {"assertions": {"type": "array", "minItems": 1}},
   "required": ["assertions"], "additionalProperties": False},
 "test.assert.ui":  {"type": "object", "properties": {"checks": {"type": "array", "minItems": 1}},
   "required": ["checks"], "additionalProperties": False},
 "test.assert.grpc":{"type": "object", "properties": {"checks": {"type": "array", "minItems": 1}},
   "required": ["checks"], "additionalProperties": False},
 "test.diff.replay": {"type": "object",
   "properties": {"failed_files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                  "v_ref": {"type": "string"}},
   "required": ["failed_files", "v_ref"], "additionalProperties": False},
 "test.snapshot": {"type": "object", "properties": {}, "additionalProperties": False},
}
```

（db/ui/grpc 的 assertions/checks 内部结构沿用各自模块 args 现有约定——实施时以 `db_assert.py:105` 等三个函数体开头对 args 的读取为准，此 schema 只封外层。）

---

## 件 5：受影响测试预扫（D7 全链路包裹的红名单）

预扫方法：`grep -rn "timeout" tests/ --include="*.py"` 全量过一遍，**直接断言 ExecResult 超时语义的点极少**（实锤：只有 `tests/test_install_deps.py:35/:145` 两个 `_FakeExecResult` 构造，不依赖真实超时路径）。真正的回归面是**间接**的：

| 风险面 | 预扫结论 | 实施动作 |
|---|---|---|
| 直接断言超时返回形态（`exit_code==-1`/`"timeout after"` 措辞） | **零个测试直接断言**（grep 无命中）——124→-1 映射 + 措辞保留后无红 | 仍需新增 4 例（设计稿 0-C 红绿单） |
| `_FakeExecResult` 假结果注入（install_deps 测试） | 与 timeout 包裹无关（fake 不经过真实 exec） | 无需改 |
| runner 真容器测试（`tests/test_runner_ensure.py` 等） | 只测镜像存在性/容器拉起，不测长命令超时 | 无需改，但**四镜像探针必须先跑**（见下） |
| 全量回归（1321 绿基线） | timeout 包裹改变**每条**容器命令的实际执行形态 | 包 0-C 实施后全量回归必须绿，这是 D7 的最终验收 |

**四镜像 coreutils 探针（实施 0-C 第一动作，全部通过才动代码）**：

```bash
# 对 mta-test:py312 / mta-test:node20-jest / mta-test:jdk17-maven / mta-test:php83 各执行：
docker run --rm <image> timeout 1 sleep 5; echo "exit=$?"   # 期望 exit=124
docker run --rm <image> timeout -k 2 1 sh -c 'sleep 30 & echo started'; echo "exit=$?"
```

任一镜像无 coreutils `timeout` → 该镜像补 `apt-get install -y coreutils` 层（Debian 系一般自带，预期零补）。

---

## 件 6：真值速查（包 4 三项目的缺陷真值与历史格号）

| 项目 | 仓库路径 | 埋缺陷形态 | 首次真检出格 | 历史需求真源（逐字复用） |
|---|---|---|---|---|
| coupon-api（python） | `data/demo/L2场景-coupon-api` | 自建 demo，**天然干净**（L2 场景/验收轨价值），真值用 clean 口径=误报率分母（`遗憾清偿设计-20261119.md:63-71` 实锤其干净地位） | #151（L4 证据链）/ #1976（pass 1.0） | 基线 #1155-1160 需求文本逐字复用（六语言报告 §设计节） |
| js-calc（javascript） | `data/demo/js-calc` | **clamp 闭区间缺陷**（上界未钳制，`多语言工具链接入任务包-20261106.md:33` 实锤：边界值用例抓到 product_defect 证人） | #1977（六语言）+ n=3 #1982 族 + ts 版 #1157 系 | 基线 #1156 文本 |
| go-rest（go） | go-rest demo 仓（六语言报告 #1980 格） | **DELETE 幂等缺陷**（六语言报告 `:47` 实锤：go 抓 DELETE 幂等，与基线逐型相同） | #1980 + 接口对账命中（4 端点） | 基线 #1159 文本 |

**真值文件格式**：`data/corpus/corpus-001/grade_taskN.json`（`planted_assertion/detected/uncertain/not/recall/extra_suspect` 字段，样例 task105：3 埋 2 检出 recall 0.67）——包 4 判据脚本直接读同构 JSON，输出到 `data/reports/agent_loop_2x2/grade_taskN.json`。

**corpus-003 注意**：`--target corpus003` 无连字符（E8 脚本参数坑，2026-11-04 实锤）——驱动器复用 driver_kit 时保留此约定。

**A 臂 sanity 对照源**：六语言报告 `docs/六语言对照实验报告-L1L2L3机制-20261118.md`（#1976-1981 表+n=3 附件）——D8 拍板=新跑 A 臂，历史数字只做"新跑结果是否在历史波动带内"的合理性检查。

---

## 附：实施当日现场确认清单（三条，五分钟）

1. server 重启加载新代码（`taskkill /F /PID <旧>` → 后台重拉，参照修复验证批操作记录）；Docker Desktop daemon 存活（`docker ps`）。
2. DeepSeek 余额；当前时段——真机冒烟/36 格实验**只在低价窗**（00:30-09:00 / 12:00-14:00 / 18:00-24:00）。
3. `python scripts/module_check.py` 全 PASS + `pytest tests/ -x -q` 基线绿（1321+）再动手。
