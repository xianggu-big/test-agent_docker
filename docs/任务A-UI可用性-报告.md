# 任务A报告：评审页三项可用性修复（2026-11-01）

## 检索记录（研究先行纪律）

| 候选 | 链接 | star/版本 | 最近更新 | License | 适配点 | 风险/不采用理由 |
|---|---|---|---|---|---|---|
| 原生 `<details>` 折叠面板 | MDN https://developer.mozilla.org/zh-CN/docs/Web/HTML/Element/details | 浏览器内置 | — | Web 标准 | 增补结果面板用原生标签，零 JS、无障碍友好 | 无（已采用） |
| data-* 分组渲染（本项目既有模式） | 仓内 app.js renderTasks 两级视图 | — | 2026-10-31 | — | 用例清单分组沿用项目自身模式，不引前端框架 | 无（已采用） |

- 检索关键词与平台：MDN（details/summary 语义）；仓库内既有分组渲染先例（renderTasks / PG_COLLAPSED）。
- 结论：本任务为纯可用性修复（改既有 DOM/契约），无新机制需要外部库，采用 Web 标准标签 + 项目既有渲染模式。检索不可用场景不适用（有网络，但无必要引入第三方方案）。

## 根因复验（与任务卡对照，全部属实）

1. **taskdetail 路由死链**：`app.js route()` L82 `PAGES.forEach` 只 toggle PAGES 六页；`taskdetail` 不在 PAGES，`#page-taskdetail` 初始 `hidden` 永远摘不掉 → `#/task/{tid}` 白屏（Playwright 实测修复前 `visibleSections=['page-new']`）。
2. **cases 只回最新脚本**：`api_cases` `ORDER BY version DESC LIMIT 1`；#281 原始 17 条在 test_gen_r1（v1），增补 15 条在 test_gen_fb1（v101）→ 页面只见其一。
3. **feedback 摘要无明细**：`run_feedback_round` 只回 `generated/file/script_id` 等数字摘要，无 name/method/expected。

## 改动清单

| 文件 | 改动 |
|---|---|
| `test_agent/api/server.py` | ① `api_cases` 增 `?all=1`：回 `{scripts:[{id,framework,version,filename,cases[]}], cases:扁平全量}`（默认路径逐字节不变）② `api_feedback` 摘要附 `cases[]`（name/method/priority/input/expected/expected_basis）+ `filename`（打桩可测，不依赖 LLM） |
| `test_agent/api/static/app.js` | ① route() 显式 toggle `#page-taskdetail`（A3 根修）② loadReview 改 `?all=1` 按脚本分组渲染（首轮生成/对话增补/人工插入 分组头 + 条数）③ sendFeedback 渲染「本轮新增 N 条」`<details open>` 面板（用例名/方法/优先级/作用=expected/依据徽章/来源文件，默认展开）④ 评审页非待审查提示按状态分化：failed=error+下一步 / 执行中=进度入口 / 准备中=轮询说明 ⑤ loadTaskDetail 失败任务 hero 显式失败框（error + 「没有报告与 RTM（不是项目不兼容）」+ 下一步）；活跃态 3s 自动轮询（TD_POLL，离开页面/终态即停） |
| `tests/test_review_api_cases.py` | 新增 5 单测：默认契约不变 / all=1 分组+扁平+契约字段 / 空态 / feedback 附明细 / 无 script_id 空数组不炸 |

## 验收证据

- **pytest**：418 passed, 1 skipped（基线 413 + 本任务 5）。
- **module_check**：136 OK / 0 MISS / 25 PASS / 0 FINDING。
- **node --check**：通过。
- **Playwright（19/19 PASS，截图 data/reports/screenshots/taskA/）**：
  1. `#/task/281` 直接访问 → `#page-taskdetail` 可见、hero 有内容、失败框含 MemoryError + 下一步、流水线有内容；
  2. `#/review/281` → 失败提示含原因、入口=「查看测试报告」（无死链）；用例清单「首轮生成（17 条）」「对话增补（15 条）」分组齐、原始用例未被藏；
  3. `#/review/270`（queued）→「任务详情→」点击后 hash=#/task/270 且页面可见；
  4. feedback 增补面板：API 路由拦截 mock（不调 LLM 不写库）→「本轮新增 2 条」面板出现，含用例名/作用/依据徽章；
  5. 全程零控制台错误。

## 前后行为对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 点「查看执行进度 / 任务详情」 | 白屏无反应（route 死链） | 跳 #/task/{tid}，详情页完整可见 |
| 反馈增补后 | 只见「✅ 增补 15 条」一行数字 | 「本轮新增 N 条」面板逐条列名字/方法/作用/依据/来源，默认展开 |
| 评审页用例清单 | 只见最新脚本（原始 17 条被藏） | 按脚本分组全量展示（首轮/增补/人工） |
| 失败任务在评审/详情页 | 「不在待审查状态——本页只读」 | error 原文 + 死在哪一步 + 下一步建议 |

## 未做项与边界

- 「查看执行进度」按钮（执行中态）无法用真实数据验收（当前无 running 任务，402 期间不跑真任务链）——代码路径与 queued 态「任务详情→」同一 href，由 [3] 间接覆盖；标注为未直接验证。
- 增补面板的真实端到端（真调 LLM）待 DeepSeek 充值后补验；当前以单测（后端契约）+ 路由拦截 mock（前端渲染）两段拼接覆盖。
