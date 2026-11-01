# 任务B报告：#281 MemoryError 修复 + 失败诊断/空态（2026-11-01）

## 检索记录（研究先行纪律）

| 候选 | 链接 | star/版本 | 最近更新 | License | 适配点 | 风险/不采用理由 |
|---|---|---|---|---|---|---|
| docker-py put_archive（文件句柄流式） | https://docker-py.readthedocs.io/en/stable/containers.html + 源码 docker/api/container.py | docker==7.2.0（已锁） | 活跃 | Apache-2.0 | data 参数收"bytes or stream"直通 requests——传真文件句柄即流式上传，无需自研分块 | 无（已采用；源码实读确认 `_put(url, data=data)` 无缓冲） |
| tarfile 流式写临时文件 | https://docs.python.org/3/library/tarfile.html | Python 标准库 | — | PSF | `tarfile.open(name=tmp, mode="w")` 直接落盘，峰值内存=单文件缓冲 | 无（已采用） |
| pathspec（gitignore 解析） | https://github.com/cpburnz/python-pathspec | 0.12.1 / 237★ | 活跃（CHANGES.rst 持续维护） | MPL-2.0（双 MIT） | `GitIgnoreSpec.from_lines` 完整 gitignore 语义（含否定模式）；本机已被 black/langgraph-cli/mypy 传递安装 | 无（已采用并显式声明进 requirements；缺库时降级只走内置排除，不炸主链路） |
| 自研 gitignore 正则匹配 | — | — | — | — | — | 否决：gitignore 语义坑多（anchored/`**`/否定/目录尾斜杠），pathspec 是事实标准且已在依赖树里 |

- 检索关键词与平台：`put_archive streaming large tar`（docker-py 官方文档+GitHub 源码）、`tarfile stream write`（Python 官方文档）、`python gitignore matching library`（GitHub）。
- 结论：流式方案全部复用已锁依赖+标准库；gitignore 解析复用 pathspec（显式声明，版本钉死 0.12.1）。

## 根因复验（与任务卡对照）

- `runner.py copy_dir_into`：`io.BytesIO` + `tarfile.open(mode="w")` 整目录内存拼 tar → put_archive。#281 工作区 7.75GB（data/ 占 7.22GB）→ 宿主 `MemoryError`，死在 provision、未进 evaluate → 无 test_levels/test_reports/evaluation_rounds/rtm_maps。**"概览/RTM 生成不出来"不是项目不兼容，是任务没跑到评估阶段**——复验属实。
- #281 工作区顶层 `.gitignore` 实测含 `data/`、`node_modules/`——尊重 .gitignore 即可把快照从 7.75GB 降到 MB 级。

## B1 快照策略（核心修复）

| 文件 | 改动 |
|---|---|
| `test_agent/config.py` | 新增 `SNAPSHOT_MAX_TOTAL_BYTES`（512MB 总预算）/ `SNAPSHOT_MAX_FILE_BYTES`（64MB 单文件）/ `SNAPSHOT_EXCLUDE_DIRS`（内置排除集，可追加） |
| `test_agent/sandbox/runner.py` | 新增 `snapshot_plan()`（纯函数预扫描：顶层 .gitignore via pathspec + 内置排除 + 单文件超限跳过记录 + 总量超限 **RuntimeError fail-fast**，错误信息含最大纳入项/总量/上限/三条处理建议）+ `_load_gitignore_spec()`（缺 pathspec 降级不炸）+ `_dir_size()`/`_fmt_bytes()`；`copy_dir_into()` 重写：预扫描 → 临时文件流式 tar → `put_archive(文件句柄)` 流式上传 → chown；返回快照摘要（文件数/字节数/跳过清单/排除清单/gitignore 是否生效）；峰值内存与工作区大小解耦 |
| `test_agent/graph/nodes.py` | provision 节点把快照摘要写 agent_messages（「N 个文件 X 进考场；尊重 .gitignore 排除 M 个目录…」）——界面可查，不再静默 |
| `requirements.txt` | 显式声明 `pathspec==0.12.1`（原为传递依赖） |

诚实边界：.gitignore 只认**顶层**文件（#281 实况已覆盖；嵌套 .gitignore 列未来工作）；空目录不入 tar（解包侧自动建父目录，无影响）。

## B4 证据

- **dry-run（只读扫描 #281 真实工作区，不碰 Docker）**：`snapshot_plan('data/workspaces/task_281')` → 纳入 **139 个文件 / 1.6MB**（预算 512MB），gitignore 生效，排除 `data/ 7.2GB`、`.workbuddy`、`.zcode`，跳过单文件 0，扫描耗时 0.6s。修复前同目录 = 7.75GB 全量进 BytesIO → MemoryError。
- **单测（tests/test_snapshot_budget.py，6 个，tmp 目录小文件+显式小预算）**：gitignore 尊重/内置排除/单文件超限跳过记录/总量超限 fail-fast 错误信息可行动/流式 tar 结构合法（FakeContainer 收流读回 tarfile 校验，monkeypatch docker.from_env）/预算默认值来自 config。
- **重跑 #281 真任务：未跑**（DeepSeek 402 期间不跑真实任务链；按任务卡以 dry-run+stub 证据替代并如实标注）。

## B2 失败诊断/空态

| 位置 | 改动 |
|---|---|
| `app.js renderVerdict` | 失败任务无报告 → 红⨯诊断卡：「任务失败——未执行到评估阶段，故无报告」+ error 原文 + 「为什么没有报告和 RTM」（明示**不是项目不兼容**）+ 三步处理建议；替代原「⏳ 暂无报告…跑完评估后给结论」（对失败任务是误导） |
| `app.js` RTM tab | 无映射快照（map_source=null）→ 行标「未映射（未评估）」中性徽章 + 徽标文案「条件已产出但评估未运行，故无映射快照——条件本身是健康的」；不再误标红色「缺口」（缺口=评估跑过发现漏覆盖，两回事） |
| `server.py api_rtm` | 无 rtm_maps 快照时用已落库条件构建 unmapped 行（conditions 不再被丢弃）；**不写 rtm_maps 表、map_source 仍 None**（不伪造快照） |

- Playwright（6/6 PASS，截图 data/reports/screenshots/taskB/）：#281 报告页判决卡=失败诊断/解释无报告原因/给处理建议/RTM 措辞正确/6 条条件显示「未映射（未评估）」/零控制台错误。

## B3 近期失败分类（只诊断）

| 任务 | 诊断 | 处置 |
|---|---|---|
| #278 `ProgrammingError: Incorrect number of bindings (uses 2, supplied 1)` | **已修**：static_scan.py 是 d35b8de（20:12）新增文件，278（11:54）踩的是提交前开发态的 test_cases INSERT 绑定数 bug；提交版 9 占位符 9 参数正确，回归测试 `test_generate_static_drafts_end_to_end` 锁死（docstring 明写"真机实锤 INSERT 绑定数错"） | 无需改码；任务可重跑验证（402 期间未跑，如实标注） |
| #277 java static_only | 诚实降级（run 节点 A7 语义，旧路径在 d35b8de 前走 run 停止） | 不改（确认） |
| #276/#274 repo_url 非法 | fail-closed 文案清楚（RuntimeError: repo_url 非法（仅支持 http(s)/gi…）） | 不改（确认） |
| #275/#280 疑似产品缺陷 | 证人语义正确（断言失败用例保留为证人，如实上报不回炉） | 不改（确认） |

## 验收汇总

- pytest：**427 passed, 1 skipped**（418 基线 + 快照 6 + RTM 3）；module_check 136 OK / 0 MISS；node --check 通过。
- 7.7GB 工作区不再 OOM（dry-run 实测 1.6MB）；超限 fail-fast 错误信息含跳过项/总量/上限/建议；任务页能解释"为什么没有报告/RTM"。

## 未做项

- 嵌套 .gitignore 解析（v1 顶层够用，列未来工作）。
- #281 重跑真任务链（402 阻塞；充值后可直接重跑验证——快照修复后工作区 1.6MB 可正常进考场）。
