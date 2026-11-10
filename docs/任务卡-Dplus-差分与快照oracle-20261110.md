# 任务卡-D+：差分与快照两种 oracle（2026-11-10）

> 用户决定：把"oracle 类型里唯一的空白"补齐——差分**与**快照两条路线都做。
> 定位：这是 oracle 维度（判据类型）的扩展，不是语言维度。补齐后你的 oracle 类型从"显式断言/契约/状态不变量/静态规则/覆盖缺口"扩到含"**参照差分/黄金快照**"——覆盖"无规格"与"重构不改行为"两大经典场景。
> 纪律：新能力旗标默认关（MTA_DIFF_ROUTE / MTA_SNAPSHOT_ROUTE）；复用既有 `run_diff_gate` 骨架与容器执行链路；每项独立 commit。

## 0. 为什么是这两条（论文定位）

- **差分测试（differential testing）**：以"参照物"为 oracle——同一输入喂给两个实现（新旧版本、两种语言实现、参照实现），输出不一致即缺陷。**无需人工写预期值**，是"无规格"场景的标准答案。
- **快照测试（snapshot / golden file）**：以"历史输出"为 oracle——首次运行录基线，之后逐字比对；适用于重构不改行为、序列化/渲染输出稳定、UI 结构回归。
- 两者共同点：**判据来自"另一个东西"，而不是来自"人对预期的手工声明"**——这正好补上你现有 oracle 谱系里缺的那一类（人工规约/模型推导/隐式关系之外的"参照物"）。

## 1. 路线一：CLI 差分（observation=cli, oracle=reference_diff）

### 1.1 前置条件（确定性判定）
- 存在可执行入口：`__main__.py` / `cli.py` / `package.json` 的 bin / `main.go` / `artisan` / 含 `if __name__` 的脚本；
- 存在可比对的参照：git 历史（HEAD~1 或指定 ref）**或**项目内提供了参照实现目录（`reference/`）。

### 1.2 实现
1. `skills/diff_route.py`：
   - `run_cli(target_cmd, inputs, ref="HEAD~1")`：在当前容器跑命令 → 在参照 ref 的临时工作区跑同一命令（复用 `copy_dir_into` + 容器内 git，注意：真机实锤容器内 git 可用但 `.git` 会被快照剥除，需 `keep_git=True` 或宿主侧 checkout 后快照——**实现时先做宿主侧方案，最简单**）；
   - 比对：`stdout`（归一化后）/`exit_code`/产物文件哈希；
2. **归一化层（关键）**：时间戳、绝对路径、随机 id、浮点精度、执行耗时 —— 用正则占位化（参考 `_norm_message` 的既有做法），否则每条都会"不一致"（假阳性洪水）；
3. 不一致时产出结构化差异（哪一行/哪一字段/期望 vs 实际），交给既有 attribution 三分类：**差分不一致 = 强 product_defect 信号**（但要区分"参照版本本身有 bug"——用双向判定：如果 ref 版本也复现同样输出，则不是本次改动引入）。

### 1.3 验收
- [x] 单测：归一化（时间戳/路径/浮点）不误报；双向判定（新引入 vs 既存差异）——tests/test_diff_route.py 11 测；**双向判定落地为更可测的工程化口径**：参照物双跑确定性校验（ref 自身不稳→ref_unstable 如实降级不冒充缺陷）+ 检出复核（cur 复核与参照一致→cur_unstable），真机演示中两道闸零触发=差异稳定
- [x] 真机：CLI 项目改动前后跑通（scripts/demo_dplus_routes.py，data/demo/cli-diff-demo v1(>50) vs v2(>=50) 边界差一）——**恰 1 处真实行为差异（2 行：value 50 status 翻转+eligible 4→5）、0 假阳性**，五类噪声（时间戳/路径/uuid/浮点/时长）全部占位化；产物 data/reports/dplus/dplus_routes_demo_*.json；演示期实锤归一化缺口并修复=含空格 Windows 路径只吃到首个空格→残余段伪差异行，补分隔符相对路径残余正则（取舍：分数 "1/2" 会被吞为 <PATH>，CLI 场景分数罕见，偏向防伪差异，docstring 注记）
- [x] 路线注册进 routes.py（observation=cli，oracle=reference_diff，前置=has_cli_entry：__main__.py/cli.py/main.go/package.json bin/py 头部 __main__ 块）
- [x] 旗标关闭时零行为变化——route_plan 默认剔除（理由可审计，单测锁）+ run_cli_diff 执行闸拒绝（双保险）；MTA_DIFF_ROUTE 默认关

## 2. 路线二：快照（observation=dom/process_output, oracle=golden_file）

### 2.1 形态
- **UI DOM 快照**：ui_assert 的可达性树已产出（"UI 版端点清单"）→ 直接存为基线 JSON，后续比对结构差异；
- **进程输出快照**：命令/函数输出序列化为 JSON/YAML 基线，逐字段比对；
- **更新机制（安全边界）**：基线更新必须显式 `--update-baseline` 或"人工确认门"，**禁止自动更新**——否则"改基线掩盖回归"就是新一代造假（对应你的防造假纪律）。

### 2.2 实现
1. `skills/snapshot_route.py`：`capture(...)` / `compare(baseline, current)` / `update_baseline(require_confirm=True)`；
2. 基线存储：`data/snapshots/<task_id|project_key>/<name>.json`（**不入 git**，但契约变更要能审阅）；
3. 差异输出：结构化 diff（新增/删除/变更的字段路径），落 test_case_results，沿用既有归因。

### 2.3 验收
- [x] 单测：结构 diff 正确（新增/删除/变更三态，含字段路径与 list 索引路径）/基线更新需确认（无确认 RuntimeError+基线未被偷改）/无基线时首跑只录不判（load=None→record）——tests/test_snapshot_route.py 9 测；**class 存排序串**（class 是无序集合，列表对齐会把重排当变更=假阳性设计缺陷，实现期自查修正）；record 已存在拒绝静默覆盖（录基线是显式动作）；旗标关=写入函数拒绝（只读 compare/load 不设闸）
- [x] 真机：687 类 HTML 页面（兔兔记账本 index.html，D:\c学习数据结构\test_1_学生管理系统）DOM 快照基线落 data/snapshots/（不入 git）→ 改一个元素 class → diff 定位到 $.children[0].children[1].children[0].class（changed，baseline 含旧 class 值）→ **对照组：未改动页面 zero diff**（重构不改行为=快照 oracle 核心价值）；无确认改基线被拒实锤
- [x] 路线注册（observation=dom，oracle=golden_file，前置=any(html≥1, has_cli_entry)——新增 any OR 组合子，理由含 ✓/✗ 各子项可审计）
- [x] 旗标关闭零行为变化——MTA_SNAPSHOT_ROUTE 默认关
- [x] 实现说明（诚实边界）：DOM 快照用 html.parser 确定性解析（零浏览器零 LLM）——静态页面 DOM=解析树；动态页面（运行时派生元素）需 playwright 路径，列扩展（ui_assert 可达性树接入留任务卡 3）

## 3. 依赖：CoverageAdapter（白盒地基，与任务卡-F2 阶段 0 同一件事）

差分/快照补的是 oracle 维度，而**多语言全绿**卡在覆盖率统一——两卡共享同一个前置：
- `skills/coverage_adapter.py`：统一 `{source, files:[{file, statements, missing_lines, branches_covered, branches_total}]}`
- 后端：coverage.py / c8 / JaCoCo / go cover / clover
- 先做这个（1~1.5 天）再铺语言，否则每个语言重复实现缺口解析

**排期建议**：CoverageAdapter → D+ 差分 → D+ 快照 → F2 阶段 1（Go）……

## 4. 论文落点

- 方法章：oracle 谱系表补两行（参照差分/黄金快照），并说明"判据来自参照物而非人工声明"；
- 实验章：差分的一次真机演示（含假阳性分析）= "无规格场景可测"的证据；
- 讨论章：快照基线的更新策略 = 防造假纪律在新 oracle 类型上的延续（这点很加分）。

## 5. 边界（不做）

- 跨语言实现互比（Python 实现 vs Go 实现同题）——研究价值高但工程大，列未来工作；
- 视觉像素级比对（截图 diff）——与"功能正确性"范围冲突；
- 差分模糊化（differential fuzzing）——列展望。
