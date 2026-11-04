# 任务 G 补充：归因三闸真机验证报告（2026-11-04 深夜）

> 计划出处：docs/11.4恢复计划.md §7 剩余①「真机开启三闸跑一次任务验证归因分布输出」。
> Task G v1（9a51ea4）的 27 个单测全部跑在 ScriptedRunner 假 runner 上——本轮用
> **真 Docker 容器 + 真图节点 + tmp SQLite**（零 LLM、不碰 data/test_agent.db）回答
> 「三闸在真机上能不能转起来」。

## 结论先行

**三闸真机全部可用且行为正确**；真机验证另抓出 **2 个会让差分闸在真实链路 100% 不可用的集成缺口**（执行镜像无 git、快照剥 .git），均已定位并修复其一，其二给出解法候选。

| 实验 | 场景 | 期望 | 真机结果 |
|---|---|---|---|
| E1n | py312 容器（无 node）跑 JS 金丝雀 | fail-fast 判环境无效，真实用例 0 次执行 | ✅ verdict=stop，exit=127，run_tests 调用 0 次，`[env-gate]` 消息留痕 |
| E1p | node20-jest 容器金丝雀 | 通过 → 继续执行链 | ✅ 金丝雀通过消息 + run_tests 正常调用 |
| E2 | **task_366 反事实回放**（见下） | 12 条全错从 product_defect 降级 case_defect | ✅ `差分闸降级（低置信）：对照 <V_ref> 上同样失败 → 非产品缺陷`，轮次行 failure_kind=case_defect，工作区切回 HEAD、未跟踪测试文件保留 |
| E3 | 真缺陷（calc 减法冒充加法）+ parity 计数器 flaky | fail-to-pass 维持 product_defect；连跑不一致标 flaky | ✅ 对照通过维持高置信缺陷；flaky 连跑 3 次 1 过/2 败 → stop_reason 如实标注 |

## E2：366 反事实——「如果当时有三闸，误诊不会发生」的直接证据

工作区构造（容器内真实 git，两提交）：
- **V_bug** = task_366 平台快照原样（questionforge 修复前产品，与平台当时执行一致）；
- **V_ref** = questionforge 真仓库 e177687 提交提取的修复后产品（lib/db.js GET_LOCK + cleandata.js stdout 双写）；
- 平台生成的 test_gen_r1.test.js（含 ROOT 脚手架 bug）以**未跟踪**形态放入（差分闸 checkout 语义下天然保留）。

真机链路：run 节点（金丝雀过）→ jest 12 条全败（`Cannot find module '/apicheck.js'`，stderr 诊断段可见）→ evaluate 判 product_defect → **差分闸**：容器内 git checkout V_ref → 复跑失败子集 → 同样 12 败 → 2×2 双败格 → `product_defect 降级 case_defect（低置信）` → checkout 切回 V_ref（已复核 HEAD 一致、测试文件仍在）。

**这正是 task_366 事故的正确判词**：连修好的产品上都照样全灭 → 锅在用例脚手架，不在产品。原链路给出的是「疑似并发产品缺陷」；三闸开启后给出的是「排除产品缺陷（用例/环境错，低置信）」——12 条误诊从机制上被阻断。

## 真机发现的 2 个集成缺口（假 runner 测不出）

### 缺口 ①：两个执行镜像都不含 git → 差分闸恒 `no_git`（已修复）

`node:20-bookworm-slim` 与 `python:3.12-slim` 均不带 git。Task G 单测全绿但真机上
`run_diff_gate` 第一步 `git -C /work rev-parse` 就会 `git: not found`（exit 127）——
**差分闸对 Python/JS 任务在原镜像上 100% 不可用**。python-test Dockerfile 头部注释
「沙箱内不跑 git」是旧设计假设，被差分闸推翻。

修复：两 Dockerfile 各加 `apt-get install git` 层并注明设计假设变更；镜像已重建
（mta-test:node20-jest 实测 `git version 2.39.5`；py312 构建同步进行）。

### 缺口 ②：copy_dir_into 内置剥 `.git` → clone 类任务的容器 /work 永远非 git 仓库（待决策）

`SNAPSHOT_EXCLUDE_DIRS`（config.py:35）内置排除 `.git`（体积/安全考量，Task B 设计）。
平台 provision 链路 = 宿主 workspace → `copy_dir_into` 进容器 → **容器内 /work 没有 .git**，
差分闸 `no_git` 如实降级（行为正确、诚实，但功能永远用不上）。

解法候选（A 轨基准接线时拍板，本验证轮不动代码）：
1. `copy_dir_into` 增加 `keep_git=False` 参数，A 轨/差分场景显式开启（含安全边界：只读 ref 操作）；
2. A 轨基准任务改走「容器内 git clone」路径（fetch_baseline 产物直接 clone 进 /work）；
3. 差分闸双通道：宿主侧 workspace 执行 git 切换 + 容器内重拷被切版本（无 .git 依赖，但多一次拷贝）。

### 附带修复：stop_reason 措辞重复拼接

E2 实测暴露 `差分闸降级…：对照 X 上对照 X 上同样失败`——nodes.py 差分闸降级分支把 ref
拼了两次（detail 自带全句）。已修（单测 40 相关全绿）。

## 复现方式

```bash
# 前置：Docker Desktop 已起（preflight.ensure 可自动拉起）；镜像已含 git 层
cd /d/test\ agent_docker
python -I data/reports/gate_verify/verify_gates_real.py
```

驱动脚本 = data/reports/gate_verify/verify_gates_real.py（四实验 + 台账/轮次行断言 +
容器清理）；每轮产物落 run_<时间戳>/（tmp 库 gates.db、两份 git 工作区）。本輪证据目录：
`run_20261104-235426/`。零 LLM 调用、零生产库触碰。

## 对计划 §7 状态的影响

- 剩余①「真机开启三闸」→ **✅ 完成**（本报告）；三闸开关开启方式即三个 config 旗标
  （ENV_GATE_ENABLED / DIFF_GATE_ENABLED / FLAKY_RERUNS），真任务实战待 A 轨语料。
- 剩余② A 轨拉取：注意缺口②——A 轨接线时必须先解决 .git 进容器问题，否则差分闸空转。
- 剩余③ 归因分布 UI、④ Go/Rust 镜像：不变。
