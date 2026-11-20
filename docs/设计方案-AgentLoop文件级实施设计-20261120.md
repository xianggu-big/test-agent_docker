# 《Agent Loop 文件级实施设计》

> 上游文档：`docs/设计方案-随llm进化可迭代可进化test系统-20261120.md`（宏观稿 v1.0，架构/契约/生命周期/验收框架）
> 配套调研：`docs/调研-AgentLoop同类方案调研-20261120.md`（开源+论文外部核验；产出 5 处设计修正待并入——tautology 闸/read 截窗/policy_deadlock 停/register 审计/漏斗指标，见其 §6 映射表；D7 获外部证据支持=全链路包裹）
> 本文定位：宏观稿的**文件级实施层**——改哪个文件、加什么函数、先红后绿用例、验收判据。
> 版本：v1.0（设计稿）　日期：2026-11-20　状态：待拍板，未实施
> 边界：本文不授权改代码、跑付费实验。所有条目均已对码（file:line 证据，对码时间 2026-11-20 深夜）。
> 用户愿景锚点：**可迭代 agent loop——Agent 自己发现问题、自己接入所需工具/插件、继续测试**；D2 已定向=批准目录内允许任务内自动安装（2026-11-20 讨论，正式拍板见 §8）。

---

## 0. 对码结论：宏观稿接入点核验 + 三处修正

实施前逐条核对了宏观稿附录 B 的接入点断言，**全部位置属实**，另发现三处需要在文件级设计中修正/收窄：

| # | 宏观稿断言 | 对码结果 | 文件级修正 |
|---|---|---|---|
| V1 | `registry.py:70` 只做确认门和超时 | ✅ 属实（`registry.py:70-98`） | **补两枚宏观稿未写的洞**：`arg_schema` 落库但**从不校验**（`register` 存了 `input_schema`，`invoke` 不读）；`skills.enabled` 列存在但 `invoke` 不查 |
| V2 | `llm_client.py:59` `__getattr__` 透传会绕过包装层 | ✅ 属实 | **MVP 不走 function calling**（宏观稿 9.2 的 `bind_tools_preserving_wrapper` 降级为阶段 2 议题）：`RobustChat` 无 `bind_tools`，调它会经 `__getattr__` 返回**裸 ChatOpenAI 绑定**——重试/402 降级/记账/ctx_budget 全丢。MVP 用 JSON action 协议 + `_loads_tolerant` 宽容解析（批4 已验证的漂移容错资产），零新风险面 |
| V3 | 可取消执行缺口= `registry.py:85-87` | ⚠️ **不完整** | **`sandbox/runner.py:303-312` 同病且更常走**：`exec_in` 也是 `_pool.submit(container.exec_run) + future.result(timeout)`，超时后 docker exec **在容器里继续跑**，还返回伪造的 `ExecResult(exit_code=-1, stderr="timeout...")`。修法必须覆盖两层（§2.3） |
| V4 | `nodes.py:1047-1049` l4 误用 L3 开关 | ✅ 属实（`:1049` `flags={"l4": config.LEVELS_L3_ENABLED}`） | `LEVELS_L3_ENABLED = False`（`config.py:97`）→ 新独立旗标默认 False=默认行为逐字节不变 |
| V5 | phase 路由"新 phase 落 other 兜底" | ✅ 属实（`llm_profile.py:315-317`） | 必须显式加组（§5.1），否则 agent_loop 调用静默走 default——`map_requirements :247` 同族坑第三起的风险，静态扫描已实锤过两起 |

其余核验：`builder.py:36-38` 节点表、`:58-61` evaluate 三路分支、`builder.py:116-133` `_after_evaluate/_after_review`（A2 冻结路由）、`routes.py:152` 11 条路线、`routes.py:233` `route_plan` 固定优先级、`builtin.py:110` `build_skills` 固定闭包、`recipe.py:24-27` 语言枚举仅 python/javascript/go、`config.py:183` `ROUTER_ENABLED` 默认关、`config.py:289-305` 安装白名单——全部与宏观稿描述一致。

---

## 1. 总包结构与依赖序

```
包 0  阶段 0 残余四项（前置加固补齐）        零 API ≈2.5 天   ← 必须最先
包 1  阶段 1 MVP（能力层+决策循环+两节点）    零 API ≈6 天     ← 依赖包 0 的 0-B/0-C
包 2  D2 落地（批准目录+R2 自动安装）         零 API ≈2 天     ← 依赖包 1 的 capabilities 契约
包 3  配套三件（phase 路由/记账/报告页）      零 API ≈1 天     ← 与包 1 同 PR 落，防止裸奔
包 4  实验协议（2×2 预注册）                  纯文档          ← 包 1 验收后执行，低价窗
```

纪律：每包先红后绿；旗标默认关=旧行为逐字节不变（对比测试锁）；高峰段只做零 API 部分，真机验收全走低价窗。

---

## 2. 包 0：阶段 0 残余四项

宏观稿 §9.1 七项前置中，四项已由 Qoder 四包落地（A1 放行门✅ / A2 图级冻结✅ / A4 三道闸✅ / 索引指纹✅ / 空审查 fail-closed✅），**残余四项**如下。

### 2.1 0-A 验证资产物化快照（A2 残余）

**现状**（对码）：`verify_fix.py:28` `case_set_fingerprint` 是整体 sha1；`builder.py:439-458` `run_task_exec` 三路指纹留痕；写入口拦截靠图路由（`builder.py:116-133`）。**缺口**：指纹只能"发现被改"，不能"恢复原样"；G9 实锤布考场物化路径存在 8 行 repr 风格差（语义零改但字节不同）——整体指纹口径无法定位是哪个文件漂了，也无法证明"物化本身无篡改"。

**设计**：

1. `create_verify_task`（`verify_fix.py:115`）扩展：创建验证任务时把冻结用例集**物化**到 `data/verify_freeze/{verify_task_id}/`——每个脚本文件原样字节复制 + `manifest.json`（每文件 `sha256 + 行数 + case 名单 + 框架`）。
2. 布考场消费：`nodes.py` provision 的 `two_stage_rebuild` 分支在 `verify_mode=1` 时**优先从快照目录重写文件**（字节级一致），DB 重生成仅作快照缺失时的兜底（此时留 `[verify-freeze] snapshot_missing` 留痕）。
3. 终态对账升级：指纹三路留痕保留，另加**分文件 sha256 对账**——漂移时能定位到具体文件与行数差，区分"物化风格差"（G9 形态，白名单豁免：行数与 case 名单一致仅 repr 差）与"实质改动"。
4. 恢复端点：`POST /api/tasks/{tid}/verify-restore` 从快照目录恢复用例集（**人工触发**，不自动改库——恢复是治理动作不是 Agent 动作）。

**红→绿**：①创建验证任务→快照目录与 manifest 存在且 sha256 逐文件可复算；②布考场后工作区文件与快照逐字节一致；③人为改一行用例→分文件对账报"实质改动"且能给出文件名；④快照缺失→兜底路径 + 留痕。4 例。

### 2.2 0-B registry 执行闸

**现状**（对码）：`registry.py:70-98` `invoke` 只有确认门+超时；`arg_schema` 存库不校验；`enabled` 不查；无风险分层（`risk_level` 落库但不影响执行）。

**设计**：`invoke` 前置四闸，顺序即拒绝优先级：

```python
# registry.py invoke 头部插入（伪码）
1) enabled 闸：SELECT enabled FROM skills WHERE name=? → 0 ⇒ denied("skill 已停用")
2) schema 闸：_validate_args(skill.arg_schema, args)（~40 行自写校验器，
   支持 type/required/enum/min/max——不引 jsonschema 第三方依赖；
   未知键一律拒绝——防模型经 args 注入内部字段）
3) 风险闸：risk_level="high" ⇒ 并入现有确认门语义（confirmed=0 拒绝）；
   low/medium 保持现状（确认门只看 requires_confirmation，行为不变）
4) （路径白名单闸归 tool_policy，registry 不重复——职责分离）
```

向后兼容：现有全部调用方（provision 的 install_deps、run_feedback_round 等）args 均在各自 schema 内=行为不变；`register()` upsert 补 `enabled` 保留（现被 ON CONFLICT 覆盖为不更新，无害）。

**红→绿**：①enabled=0 → denied；②缺 required 键 → denied 且错误消息含缺失键名；③未知键 → denied；④low risk 未确认 → 照常执行（现状锁）；⑤schema 为空 dict → 跳过校验（存量 skill 兼容）。5 例。

### 2.3 0-C 可取消执行（两层）

**现状**（对码）：`registry.py:85-94` 线程池超时只停等待；`runner.py:303-312` `exec_in` 同病且**更常走**（全链路容器命令都过这里）——超时后容器内进程继续跑、消耗资源、可能继续写产物。

**设计**：

1. **`exec_in` 层（根治）**：命令包 coreutils `timeout` 前缀——`exec_in(container, cmd, timeout_s=N)` 改为实际执行 `["timeout", "-k", "5", str(N), *cmd]`，外层 `future.result(timeout=N+15)` 保留作兜底。进程级真终止；`timeout` 退出码 124 映射回现有 `-1` 语义（`ExecResult.exit_code=-1, stderr=...timeout...` 措辞不变）——**对外行为逐字节不变，内部真杀死**。Linux 镜像全系带 coreutils（py312/node20-jest/jdk17/php 均 Debian 系，已验证风险低；实施时对四镜像各跑一条 `timeout 1 sleep 5` 探针确认）。
2. **registry 层**：`_pool` 线程超时后，线程内函数因 1) 已有进程级超时，线程自然退出；宿主侧纯 Python 操作（文件遍历等）补 `cancel_event` 可选协议——`Skill.func` 签名不变（兼容），`invoke` 用 `inspect` 探测 func 是否接受 `cancel_event` 关键字，接受则注入；FutureTimeout 时 `set()`。builtin 全家逐步接入（MVP 只接 `install_deps`，其余阶段 1 随 capability 化迁移）。

**红→绿**：①`exec_in` 跑 `sleep 30` timeout 2s → 2-3s 内返回且容器内 `sleep` 进程已死（`exec ps` 验证）；②timeout 退出码映射 =-1 语义（现有测试口径锁）；③带 cancel_event 的 skill 超时 → event 已 set；④不带 event 的存量 skill → 行为不变。4 例。

### 2.4 0-D 任务级密钥范围（最小口径）

**现状**：key 全局（`app_settings` / profile `key_env`）；D 包已做端点白名单/UNC 拒绝/缓存隔离。**Agent loop 新增的风险**=模型经观察/编辑工具摸到 profile 或 key。

**设计**（最小口径，不建密钥管理体系）：

1. MVP 能力清单（§3.1）`permissions.secrets` 恒 `[]`——十二项能力无一需要密钥；`tool_policy.authorize` 硬闸：args/观察空间出现 `api_key|key_env|token|secret` 键名 → denied（键名黑名单正则）。
2. `test.observe.read` 路径白名单=workspace + artifacts 两目录的**相对路径**；`data/`（含 `data/app_settings` 所在库文件）、`config/`、绝对路径、`..` 逃逸一律拒绝。
3. LLM 调用本身走 `make_chat` 内部解析（现状）：模型输入输出永远不含 key——此为既有事实，设计仅声明不改动。

**红→绿**：①args 带 `api_key` 键 → denied；②read 请求 `data/test_agent.db` → denied；③read 请求 `src/app.py`（workspace 相对）→ 放行。3 例。

---

## 3. 包 1：阶段 1 MVP 文件级设计（核心包）

### 3.1 `test_agent/skills/capabilities.py`（新，~250 行）

**Capability dataclass**（宏观稿 §6.1 字段的 MVP 精简版）：

```python
@dataclass
class Capability:
    id: str                  # "test.run.unit" 稳定标识
    version: str             # "1.0.0"
    kind: str                # observe / execute / edit / install
    languages: list[str]     # ["python"] / ["*"]
    description: str         # 菜单展示一句话
    inputs_schema: dict      # 复用 0-B 的 _validate_args 方言
    outputs_fields: list[str]# Result 结构自描述（status/metrics/evidence 恒有）
    evidence_kinds: list[str]# 必须产出的证据类型；缺失 ⇒ 结果降级 unsupported
    permissions: dict        # {"fs": [...], "network": "none", "secrets": []}
    risk: str                # R0-R4（宏观稿 §6.3）
    timeout_s: int
    compat_images: list[str] # 适用镜像（空=不限）
    handler: Callable        # (task_ctx, args) -> CapResult
```

**CapResult 统一结构**（宏观稿 §6.2 铁律落地）：

```python
@dataclass
class CapResult:
    status: str              # ok / failed / timeout / denied / unsupported
    summary: str             # ≤200 字摘要（进观察空间）
    metrics: dict            # 解析器产出的数字（passed/failed/coverage…）
    evidence: list[dict]     # [{"kind","path","sha256"}]——任务产物目录下真实文件
    diagnostics: list[dict]  # [{"code","message"}] 结构化诊断码
    raw_exit: int | None
# 铁律实现：handler 内部 verdict 一律不写；metrics 只能来自解析器
# （test.run.unit 复用 case_results.py 解析），模型摘要进 summary 不进 metrics
```

**BUILTIN_CAPABILITIES 十二项与现有代码的映射**（全部踩在已有函数上，无新执行面）：

| 能力 id | 风险 | 复用什么 | handler 形态 |
|---|---|---|---|
| `test.observe.read` | R0 | 新写 ~40 行：workspace/artifacts 相对路径读/列/grep（0-D 白名单） | 宿主侧只读 |
| `test.observe.env` | R0 | `skills/stack_detect.py` 探测子集（语言/解释器版本/依赖清单存在性） | 容器 exec 只读命令 |
| `test.run.unit` | R0 | `runner.exec_in` + `case_results.py` 解析（与 run 节点同命令形态，targets 子集参数化） | 容器执行 |
| `test.observe.coverage` | R0 | `state.per_file` 台账 + `coverage_check.py` 缺口行 | 纯读 state/DB |
| `test.observe.logs` | R0 | `last_stdout/stderr` 截尾 + `agent_messages` 近 N 条摘要 | 纯读 state |
| `test.gen.cases` | R1 | `gen_tests.generate_tests` 包装（定向 targets/feedback 参数） | 现有生成链 |
| `test.edit.scaffold` | R1 | 新写 ~60 行：写 `test_gen_scaffoldN.*` + **diff 分类闸**（§3.6） | 容器/宿主写测试产物 |
| `test.assert.db` | R1 | `builtin.py` db_assert 同源逻辑 | 容器执行 |
| `test.assert.ui` | R1 | `builtin.py` ui_assert（ui_gen 生成 + Playwright 执行） | 容器执行 |
| `test.assert.grpc` | R1 | `builtin.py` grpc_assert | 容器执行 |
| `test.diff.replay` | R0 | `attribution.run_diff_gate`（要求 .git；no_git 如实降级） | 容器执行 |
| `test.snapshot` | R0 | snapshot 路线函数（旗标联动 `SNAPSHOT_ROUTE_ENABLED`，关=capability 不入菜单） | 容器执行 |

`capability_menu(task_ctx)`：按 `primary_language` / `image` / `compat_images` / 旗标过滤 → 返回 `[{id, description, risk, inputs 简述}]`（**菜单只给摘要**，完整 schema 在 authorize 后由 executor 持有——渐进披露，控 token）。

### 3.2 `test_agent/skills/tool_policy.py`（新，~220 行）

```python
def authorize(action: dict, task_ctx: TaskCtx) -> Decision:
    """硬约束全判定。返回 Decision(allowed: bool, reason: str, risk: str)。
    拒绝路径全部留痕（agent_evidence 追加 {"kind": "denied", ...}）。"""
    七道闸（顺序=拒绝优先级）：
    1) 协议闸：action 必须 {"action": "call"|"finish", ...}——call 缺 capability/args ⇒ denied
    2) 存在闸：capability 在菜单内（未注册/语言不兼容/旗标关 ⇒ denied）
    3) schema 闸：args 过 _validate_args（复用 0-B）
    4) 密钥闸：0-D 键名黑名单 + permissions.secrets 恒空校验
    5) 模式矩阵闸：宏观稿 §6.3——verify_mode=1 ⇒ 仅 R0 且 read-only 执行；
       static_only ⇒ 仅 R0；normal ⇒ R0/R1 自动、R2 目录内（包 2）、R3+ 拒
    6) 预算闸：agent_steps ≥ max_steps / token / 秒数 任一触顶 ⇒ 停环（非 denied——
       是 stop 条件，返回 Decision(stop=True)）
    7) 重复闸：动作指纹在已见集合出现 ≥2 次 ⇒ stop("repeated_action")
```

软启发式通道（MVP 只留痕不生效）：`record_soft_override(task_ctx, heuristic, evidence)` 写 `agent_messages`——为阶段 2"模型凭证据申请覆盖粗糙正则"预埋数据面。

### 3.3 `test_agent/skills/tool_loop.py`（新，~260 行）

```python
def run_tool_loop(deps, task_ctx, goal: str, mode: str, budget: dict) -> dict:
    """有界决策循环。mode ∈ {"strategy", "diagnose"}——同一循环两个入口参数化，
    diagnose 用更小预算（3 步）。返回 {steps, evidence, stop, strategy|diagnosis}。"""
    seen_fp, new_evidence_streak = {}, 0
    for step in range(budget["max_steps"]):
        obs = build_observation(task_ctx, evidence_tail=3)     # token 封顶 §3.7
        action = llm_decide(obs, menu, budget, mode)           # §3.4 协议
        if action is None:                                     # 两次不可解析
            return stop("hard_error", "模型输出不可解析×2（原始已留档 llm_parse_fail/）")
        if action["action"] == "finish":
            return stop("goal_met", action)
        decision = tool_policy.authorize(action, task_ctx)
        if decision.stop:  return stop(decision.reason)
        if not decision.allowed:
            evidence.append(denied_record(action, decision)); continue
        result = execute_capability(deps, task_ctx, action)    # CapResult
        evidence.append(normalize(result))                     # 摘要+证据指纹入库
        if evidence_fingerprint(result) not in seen_fp:
            seen_fp[evidence_fingerprint(result)] = 1; new_evidence_streak = 0
        else:
            new_evidence_streak += 1
            if new_evidence_streak >= 2: return stop("no_new_evidence")
    return stop("budget", "步数耗尽——已获证据如实保留")
```

- **动作指纹**：`sha1(capability + canonical_json(args))[:12]`（args 键排序后序列化——防同义重排绕过重复闸）。
- **记账**：每步 LLM 调用走 `make_chat(phase=…, task_id=…)` 现有 `log_llm_call`——`llm_calls` 表零改动即有明细。
- **fail-closed**：循环任何异常 → 返回 `stop("hard_error")` + 已有证据保留，**绝不部分重试**（外层图的 repair_env/repair_gen 语义兜底）。

### 3.4 模型输出协议（JSON action，不走 function calling）

**决策（V2 修正）**：MVP 用提示词约束的 JSON 协议，不引入 `bind_tools`。

```
SYSTEM：能力菜单（id/一句话/风险级）+ 硬约束清单 + 证据纪律（每个结论必须引用 evidence）+
        输出协议："只输出一个 JSON 对象，schema 见下"，附 call/finish 两形态示例。
USER：   观察包（goal + 任务上下文 + 近 3 条证据摘要 + 剩余预算）。
```

- 解析：`_loads_tolerant`（批4 落地的漂移容错：片段级受控求值）+ 不可解析重试 1 次 + 原始留档 `llm_parse_fail/`（既有目录）。
- 协议校验失败（合法 JSON 但不是 action 形态）视同不可解析——不猜语义。

### 3.5 `test_agent/graph/agent_nodes.py`（新，~220 行）

**strategy 节点**（主图 analyze→strategy→plan）：

```python
def strategy_node(state) -> dict:
    # 触发前置（builder 条件注册已保证只在旗标开时进图）：
    #   static_only 任务不进（保持 Tier3 现状）
    # 输入：索引命中（index_scope_paths/index_endpoints）+ 模块画像 + route_plan 干跑
    #   （routes.py route_plan 传 flags 全开取"理论上可选路线全集"，不落库）
    # 输出：run_tool_loop(mode="strategy", budget={max_steps:8, tokens:30k, seconds:600})
    #   → state["agent_strategy"] = {priorities, capability_plan, budget_request, fallback}
    # 366 教训：state[...] 与 out[...] 双写（跨节点键必须进返回值）
    # 留痕：msg [strategy] 步数/停止原因/策略摘要
    # 策略不直接驱动任何执行——plan 节点读 agent_strategy 作上下文（prompt_library 新段）
```

**diagnose 节点**（evaluate 的 repair_gen 分支前）：

```python
def diagnose_node(state) -> dict:
    # 触发：verdict==repair_gen 且 round_no ≤ AGENT_LOOP_DIAG_MAX_ROUNDS(=2)
    #   （诊断只在早期轮次——后期轮次预算留给补测本身）
    # 只读小循环：mode="diagnose", budget={max_steps:3, tokens:10k}
    #   菜单收窄为 R0 五项（read/env/run.unit 子集/coverage/logs）
    # 输出：state["agent_diagnosis"] = {kind, evidence_refs, suggested_action, confidence}
    #   kind ∈ 脚手架/环境/产品缺陷/预期争议（与 defect_analysis 分类对齐）
    # 铁律：不改 verdict、不写 verdict——诊断结论注入 generate 提示词
    #   （prompt_library 新段"## 上一轮失败诊断"，expected_basis 纪律同源）
```

### 3.6 脚手架修复 vs 断言变更分类闸（宏观稿 §6.5 落地）

`capabilities.py` 内 `_classify_edit_diff(old: str, new: str) -> str`，确定性规则（AST 级，非正则）：

| diff 分类 | 判定 | 处置 |
|---|---|---|
| `import/layout` | 仅 import 段/路径/包布局变化（Python `ast.Import` 对比；JS require/import 行对比） | 允许（R1） |
| `fixture/wiring` | 夹具/启动/等待/序列化变化（函数名与断言数不变，非断言行改动） | 允许（R1） |
| `assertion` | 断言表达式或预期字面量变化（断言行 diff 非空） | **拒绝** → 生成 `oracle_change_request` 记录（agent_messages + 报告页可见），进人工审查 |
| `deletion` | 删除测试函数/skip 装饰器新增 | **拒绝** → 只允许"隔离标注"（@pytest.mark.skip + reason 进报告） |

- 旧文件、diff、原因全部留档 `artifacts/task-{tid}/scaffold/`；报告页可追溯。
- 红例金样：369 的 vm.runInContext 重组链形态作 `assertion` 类负例测试。

### 3.7 状态/配置/提示词接线

**`state.py` 九字段**（`TaskState` 追加，全部带 366 教训注释）：

```python
agent_loop_enabled: bool          # builder init 注入（config.AGENT_LOOP_ENABLED 且非 static）
agent_strategy: dict              # strategy 节点产物（priorities/capability_plan/fallback）
agent_steps: int                  # 已用决策步数（strategy+diagnose 合计，预算共享）
agent_budget: dict                # {max_steps, max_tokens, max_seconds, used_tokens, used_seconds}
agent_evidence: list[dict]        # 证据引用（kind/path/sha256/summary）——不存原始大文本
agent_diagnosis: dict             # diagnose 产物（kind/evidence_refs/suggested_action/confidence）
agent_capability_grants: list[str]# 本任务获批能力 id 清单（MVP=菜单全集；包 2 起接目录 grant）
agent_plugin_requests: list[dict] # 能力缺口申请（阶段 2 写入方；先声明防 schema 二次改动）
agent_loop_stop: str              # goal_met/budget/no_new_evidence/repeated_action/hard_error
```

**`config.py` 新增**（全部默认关/保守值）：

```python
AGENT_LOOP_ENABLED = _flag("MTA_AGENT_LOOP", False)
AGENT_LOOP_MAX_STEPS = int(os.environ.get("MTA_AGENT_LOOP_STEPS", "8") or 8)
AGENT_LOOP_MAX_TOKENS = int(os.environ.get("MTA_AGENT_LOOP_TOKENS", "30000") or 30000)
AGENT_LOOP_MAX_SECONDS = int(os.environ.get("MTA_AGENT_LOOP_SECONDS", "600") or 600)
AGENT_LOOP_DIAG_STEPS = int(os.environ.get("MTA_AGENT_LOOP_DIAG_STEPS", "3") or 3)
AGENT_LOOP_DIAG_MAX_ROUNDS = int(os.environ.get("MTA_AGENT_LOOP_DIAG_ROUNDS", "2") or 2)
LEVELS_L4_ENABLED = _flag("MTA_LEVELS_L4", False)   # V4：l4 独立开关（默认 False=现状逐字节）
```

**`builder.py` 图改动**（条件注册，关=图同现状）：

```python
# build_graph 内：
if config.AGENT_LOOP_ENABLED:
    g.add_node("strategy", nodes["strategy"]); g.add_node("diagnose", nodes["diagnose"])
    g.add_edge("analyze", "strategy"); g.add_edge("strategy", "plan")
    # evaluate 条件边换 _after_evaluate_agent：repair_gen 且未达诊断轮上限 → "diagnose"
    g.add_edge("diagnose", "generate")
else:
    （现状边逐字节——analyze→plan 直连）
# 关键：节点不注册时条件边映射表也不含 diagnose key——LangGraph 编译期即锁定两种图形态，
# 各跑一遍 compile 断言节点集合（对比测试锁）
```

- 执行图 `build_exec_graph`：同样条件接 diagnose（R-Loop 在阶段二）；**strategy 不进准备图**（阶段一考纲是人审的，策略建议反而添乱）。
- `run_task` / `run_task_exec` init 注入 `agent_loop_enabled`。

**`nodes.py` 最小改动**：`plan` 读 `state.get("agent_strategy")` 注入提示词（prompt_library 新段）；`generate` 读 `agent_diagnosis` 注入"## 上一轮失败诊断"段；顺带修 V4（`:1049` l4 旗标换 `LEVELS_L4_ENABLED`）。

**`prompt_library.py`**：`AGENT_STRATEGY_SYSTEM / AGENT_STRATEGY_USER / AGENT_DIAGNOSE_SYSTEM / _agent_strategy_section / _agent_diagnosis_section`（无策略/无诊断=空串，输出逐字节不变——`_endpoint_gap_section` 同款纪律）。

### 3.8 测试（先红后绿，24 例）

| 文件 | 用例 |
|---|---|
| `tests/test_capabilities.py` | 8：菜单语言过滤/旗标过滤/inputs 校验拒/evidence 缺失降级 unsupported/metrics 只能来自解析器（金样：伪造 metrics 抛）/edit diff 四分类（import 允许·断言拒·删除拒·夹具允许）/路径白名单/read 相对路径 |
| `tests/test_tool_policy.py` | 10：七闸各 1 例+verify 模式 R1 拒+预算触顶 stop+重复指纹 stop |
| `tests/test_tool_loop.py` | 8：步数耗尽/goal_met/两次不可解析 fail-closed/连续无新证据/拒绝留痕/动作指纹键排序/finish 透传/异常 stop |
| 图锁 | 2：旗标关 compile 节点集合与现状一致；开=多两节点（`test_agent_nodes.py` 附带） |

---

## 4. 包 2：D2 落地设计（批准目录 + R2 自动安装）

### 4.1 拍板记录

D2=**允许目录内固定版本任务内自动安装**（用户 2026-11-20 讨论定向；摘要进 AGENTS.md）。安全交换：自主性换来的是"目录预置越丰富，Agent 实际体验越自治"——目录建设成为运营动作而非代码动作。

### 4.2 目录与第一批条目

`data/tool_catalog/catalog.json`（schema=宏观稿 §7.1）。**第一批 5 条目全部是真实痛点驱动**（每条注明来源格）：

| id | 版本锚 | 来源痛点 | 形态 |
|---|---|---|---|
| `tool.parse.jacoco-multimodule` | 自研脚本 v1 | microservices 格多模块 jacoco 熔断（存疑 35 姊妹） | 平台侧解析器，容器外跑 |
| `tool.spec.swag` | swag v1.16.x | Go 契约轨 spec 提取缺口（黑盒缺口清单①） | 任务容器内 CLI |
| `tool.coverage.pcov` | pcov + deprecations 组合 pin | php xdebug 覆盖开销大（pcov 注入挂账） | 任务容器内 PHP 扩展 |
| `tool.mutate.gremlins` | gremlins v0.5.x | Go 变异白盒缺（W5） | 任务容器内 CLI |
| `tool.mutate.infection` | infection ^0.27 | PHP 变异白盒缺（W5） | 任务容器内 CLI |

条目必备：固定版本（禁 latest）/来源域名/摘要/许可证/entrypoint/permissions/fixtures（happy_path+known_bad+timeout 三类金样）/status。

### 4.3 安装执行细节（关键设计：任务容器保持无外网）

```
预拉取（平台侧，专用工具容器，网络白名单=pypi.org|npmjs.org）：
  fetch_wheel(catalog 条目) → data/tool_cache/{id}-{version}/（wheel/tar 原样）
  → 算 sha256 与目录 digest 比对 → 不符拒绝并告警（供应链闸）
安装（任务容器，零外网）：
  runner.exec_in(["pip","install","--no-index","--find-links","/toolcache",f"{pkg}=={ver}"])
  （/toolcache 只读挂载 data/tool_cache——任务容器网络保持 none）
验证：契约 fixture 三类（正常样例/已知缺陷样例/超时）——只跑正常路径不算过
登记：tool_installations 行（task_id/catalog_id/version/status）
回滚：任务容器任务结束即销毁=天然回滚；目录级回滚=status 变更（deprecated/revoked）
```

**新表**（第 40-42 表，schema 迁移走 `_MIGRATIONS`）：
- `tool_catalog`：目录条目镜像（id/version/digest/status/permissions_json）——目录文件为真源，表为可查询副本
- `tool_installations`：`id/task_id/catalog_id/version/status(installed|verified|failed|rolled_back)/installed_at`
- `plugin_proposals`：模型缺口申请（capability_needed/evidence/task_id/status(pending|approved|rejected)）——**只申请不安装**

### 4.4 新文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `test_agent/skills/plugin_manager.py` | ~200 | 目录解析/预拉取+摘要校验/任务级安装/契约验证调度/回滚 |
| `test_agent/skills/plugin_runtime.py` | ~150 | 插件以 Capability 形态动态注册进菜单（capabilities.py 的加载来源之二）；原生输出→CapResult 归一 |
| `scripts/verify_plugin.py` | ~120 | 三类 fixture 契约验证 CLI（admin 用） |
| `tests/test_plugin_manager.py` | 8 例 | 摘要不符拒/未知 id 拒/URL 形参拒/预拉取落盘/安装登记/回滚/fetch 失败留痕/proposal 只申请 |
| `tests/test_plugin_runtime.py` | 6 例 | 动态注册进菜单/输出归一/超时终止(0-C)/无网络执行/坏 fixture 禁用/ revoked 条目即刻不可见 |

### 4.5 与宏观稿的对齐修正

- 宏观稿 §7.2 步骤 3"审批"对 R2 目录内条目**合并进步骤 2**（目录批准=批量预授权）；R3（目录外/新网络/新凭据）维持人工。
- MCP 适配（宏观稿 §7.4）留阶段 2 后半，本文不展开。

---

## 5. 包 3：配套三件（与包 1 同 PR）

### 5.1 phase 路由组（V5 坑预防）

`llm_profile.py:294` `PHASE_GROUPS` 追加：

```python
"agent_loop": ["agent_loop", "agent_strategy", "agent_diagnose"],
```

routing 默认空=三 phase 走 default 档（行为不变）；admin UI 的 llm-routing 页零改动即可独立调档（组在 `PHASE_GROUPS` 即被 `set_routing` 接受，`llm_profile.py:407-413`）。

### 5.2 记账与聚合

- `llm_calls`：零改动（phase 原始值自动落）。
- `scripts/eval_metrics.py`：聚合新键 `agent_loop_tokens / agent_loop_steps / agent_denied_count / agent_evidence_count`；markdown 报告加一节「Agent Loop 账本」。

### 5.3 报告页决策轨迹

- 新端点 `GET /api/tasks/{tid}/agent-loop`：读 `agent_evidence`（state 落库副本）+ `skill_invocations`（capabilities 执行走 registry.invoke 审计台账——**capability handler 统一经 Skill 包装执行**，零第二套审计）。
- 前端 `taskdetail` 新增折叠面板「Agent 决策轨迹」：步数时间线（step/action/capability/risk/status/证据链接/denied 原因）；`agent_loop_enabled=0` 的任务面板不出现（诚实空态）。

---

## 6. 包 4：实验协议（预注册，包 1 验收后执行）

### 6.1 2×2 设计

|  | 固定流程（AGENT_LOOP=0） | 自主流程（=1） |
|---|---|---|
| 当前模型（deepseek-chat） | A=基线（**D8 拍板=重跑**：当前代码+旗标关，9 格 ≈¥0.3——四臂同版代码） | B |
| 更强模型 | C（deepseek-reasoner；复用 E10 R1 客户端设计） | D |

- 固定变量：同一项目快照/同一需求文本（逐字复用既有格——六语言对照的纪律）/同温度采样/同一评测语料与真值（corpus_truth）/同一预算（8 步 30k tok）。
- 项目（D4 拍板）：coupon-api（python）/ js-calc（js）/ go-rest（go）——全部有真机证据+埋缺陷真值，"真缺陷仍被检出"判据有现成对照。
- n=3/格；**四臂×3 项目×3 重复=36 格**（含 A 臂重跑）；历史数据（六语言对照+n=3）仅作 sanity 对照与 A 臂新跑的一致性参照，不进主对比表。

### 6.2 预注册判据（阶段 1 表，全部可脚本测量）

| 判据 | 阈值 | 测量源 |
|---|---|---|
| 策略有效率 | 100%（策略输出全过存在性/预算/硬约束校验） | agent_strategy × tool_policy 重放校验脚本 |
| 未授权成功 | 0 | skill_invocations denied 复核 + 审计 |
| 证据完整度 | ≥95% | 结论数 vs 有 evidence 引用数（eval_metrics 新键） |
| 缺陷检出 | 不低于基线 A（逐缺陷型对照） | corpus_truth |
| 回归 | 0（旗标关全量绿+逐字节锁） | pytest + 图锁 |
| 成本 | 每格 ≤ 预算（30k tok + 停车线） | llm_calls |

### 6.3 预算与执行窗

- 单格估算：8 步×约 2-4k tok/步 ≈16-32k tok ≈ ¥0.03-0.07/格（DeepSeek chat 价）；A 臂固定流程 ≈¥0.03/格 → 36 格 ≈ **¥1.5-2.5，停车线 ¥4**。
- reasoner 臂（C/D）思考链 token 重——单独估算（E10 R1 计价口径），超停车线即停。
- 全低价窗执行（00:30-09:00 / 12:00-14:00 / 18:00-00:30），驱动器复用 `driver_kit.py` v2（ensure_server/ensure_docker/create_and_run 响应码校验）。

### 6.4 工具扩展实验（阶段 2 后，独立小对照）

不与 2×2 混（归因干净）：同项目「目录内插件开/关」2×1×n=3——D2 主张（Agent 自插工具提升适配成功率）的直接检验；判据=适配成功率（新框架项目完成率）+ 检出不降 + 拒绝路径全留痕。

### 6.5 诚实边界

n=3 报均值±范围不称零抖动；D 不优于 C 时如实报告"自主循环无增量"并回退收窄；reasoner 臂若因计价/漂移不可行，降级为"模型档位差对照"（chat vs 本地 qwen 大落差臂）并声明。

---

## 7. 实施顺序与工作量总账

| 序 | 包 | 量 | API 成本 | 里程碑判据 |
|---|---|---|---|---|
| 1 | 包 0（0-A→0-D） | ≈2.5 天 | 0 | 四项各自红绿+全量回归绿 |
| 2 | 包 1+包 3（同 PR） | ≈6-7 天 | 0 | 旗标关逐字节锁+24+新增例绿+单格真机冒烟（低价窗，¥0.1 内） |
| 3 | 包 2 | ≈2 天 | 0 | 插件 8+6 例绿+第一条目录条目（jacoco 解析器）真机走通七步 |
| 4 | 包 4 执行 | 36 格（含 A 臂重跑） | ≈¥1.5-3 | 预注册判据表落盘 `data/reports/agent_loop_2x2/` |

**执行序确认（2026-11-20 用户拍板"没有要调整的地方"，含挂账合并）**：

```
0. 实施附录先行（零 API）：复用接口速查 / 新表 DDL+守卫表数契约同步 / 提示词 v0 草稿
   / 12 项 inputs_schema / 受影响测试预扫 / 真值速查
1. 包 0（残余四项）
2. 包 1+3（MVP+配套）→ 单格真机冒烟（低价窗 ¥0.1 内）
3. 包 2 —— 吸收挂账「pcov 注入」：pcov 即目录首条 tool.coverage.pcov，装完=D2 首个真实案例
4. 包 4（2×2 三十六格；挂账「G8 长链占位格」并此批低价窗顺带做）
5. 全部验收跑通后 → 解冻 Wave2 300 格（同版代码+旗标关跑，避免版本漂移重跑）
```

- 保持挂账不动：G6 恢复校验补丁、java 同族对称（存疑 35 姊妹）——AgentLoop 间隙或用户点名再做。
- Wave2 冻结理由（用户 2026-11-20）：等 AgentLoop 设计落地并验收跑通再测，避免跑完又改代码导致数据版本漂移。

## 8. 拍板记录（D1-D8 全部落定，2026-11-20）

> D1-D6 于 2026-11-20 随整体执行序确认（用户"没有要调整的地方"）按建议值落定；D7/D8 单独拍板见各行。

| 编号 | 问题 | 本文建议 |
|---|---|---|
| D1 | strategy 位置 | plan 前（§3.5 已按此设计；plan 职责不变只加上下文段） |
| **D2** | R2 目录内自动安装 | **允许**（§4.1 已按此设计；目录外首次人工） |
| D3 | MVP 预算默认值 | 8 步/30k tok/600s；诊断 3 步（§3.7 已定，env 可调） |
| D4 | 三项目 | coupon-api / js-calc / go-rest（§6.1） |
| D5 | MCP 进阶段 2？ | 阶段 2 后半（§4.5） |
| D6 | 技能格式 | 兼容 SKILL.md frontmatter 思路，平台自解析（阶段 3 议题，本文不展开） |
| **D7** | 0-C 的 coreutils timeout 包裹范围 | ✅ **已拍板（2026-11-20 用户）：全链路**——`exec_in` 本体包裹，存量链路假超时一并根治；退出码 124→-1 映射保语义；实施前预扫 `tests/` 对 ExecResult 的断言（实施附录待补项 8） |
| **D8** | 包 4 的 A 臂数据源 | ✅ **已拍板（2026-11-20 用户）：重跑**——当前代码+旗标关重跑 A 臂 9 格（≈¥0.3），四臂同版代码归因干净；历史数据（六语言对照+n=3）仅作 sanity 对照不进主表 |

## 9. 风险与对策（文件级补充）

| 风险 | 对策 |
|---|---|
| JSON 协议漂移（DeepSeek 输出不合规） | `_loads_tolerant`+重试 1+留档+fail-closed（批4 资产复用）；不猜语义 |
| strategy 节点空转烧 token | 菜单渐进披露+观察包封顶（§3.7）+步数预算；n=3 实测后调 |
| edit.scaffold 误分类断言变更 | AST 级分类+369 金样负例+拒绝即 oracle_change_request 人工审——宁拒勿放 |
| 图双形态漂移 | compile 期节点集合断言锁（旗标两态各锁一次） |
| 目录条目本身有 bug | 三类 fixture 契约验证禁用机制（verified 才入菜单）；revoked 即刻不可见 |
| 与 R-Loop 预算打架 | agent_steps 与 repair 轮次解耦（diagnose 只在 round≤2）；总 token 记账同表可审计 |

## 10. 明确不做（本设计范围内）

不给模型宿主 shell / 不 import 插件进 FastAPI 主进程 / 不自动装 latest / 不改 verdict 写权 / 不做向量检索 / 不在高峰段跑实验 / A 臂不伪造"重跑"（引历史必须注明版本边界）。
