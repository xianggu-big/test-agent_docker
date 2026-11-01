# 任务C报告：实时执行看板（逐用例过程与结果）（2026-11-01）

## 检索记录（研究先行纪律）

**存储方案对比（C1 要求"方案 A 新表 vs 方案 B JSON 列，选最小且可审计者"）**

| 候选 | 链接 | star/版本 | 最近更新 | License | 适配点 | 风险/不采用理由 |
|---|---|---|---|---|---|---|
| 方案 A：新表 test_case_results（第 24 张） | 仓内 schema | — | — | — | SQL 确定性可审计、可查询、与 evaluation_rounds 同 delete-first 幂等语义 | 代价=表数契约 23→24 同步（已同步 module_check/test_db/test_two_stage/README 口径）——**采用** |
| 方案 B：复用 agent_messages JSON / evaluation_rounds 加列 | 仓内 | — | — | — | 零表变更 | **否决**：项目自己的 P1-③ 教训——collect_metrics 正则解析叙述消息脆到"措辞一变即静默清空"，被 evaluation_rounds 表根替；逐用例明细进散文=重蹈覆辙 |

**结果源解析器**

| 候选 | 链接 | star/版本 | 最近更新 | License | 适配点 | 风险/不采用理由 |
|---|---|---|---|---|---|---|
| pytest --junitxml（内置） | https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files | pytest 内置 | 随 pytest | MIT | 零新依赖；stdlib xml.etree 解析；受控集成测试吃真 pytest 输出验证 | 无（**采用**） |
| pytest-json-report | https://pypi.org/project/pytest-json-report/ | 1.5.0 | 2022-03（三年未更） | MIT | 逐测 JSON 更细（setup/call/teardown 分段、stdout 捕获） | **不采用**：三年未维护、仅支持 py3.7-3.10；内置 junitxml 已含本任务需要的全部字段 |
| jest --json（官方 CLI） | https://jestjs.io/docs/cli#--json | jest 内置 | — | MIT | assertionResults 含 fullName/status/duration/failureMessages；仓内 js_adapter 已实测同源数据（Tier2 上线时真机验证过） | 无（**采用**，零改动） |

**实时通道**

| 候选 | 适配点 | 不采用理由 |
|---|---|---|
| 轮询（2s，fetch 新端点） | 零新机制，与项目"不引前端框架"约束一致 | **采用** |
| SSE（FastAPI StreamingResponse） | 服务端推送 | 否决：本轮粒度=轮次级（每轮跑完才出新数据），SSE 在此粒度无增益；且与常驻容器的执行模型（exec 阻塞到轮次结束）配合复杂化 |

## 实现

| 层 | 文件 | 改动 |
|---|---|---|
| 数据 | `docs/layer5-data-schema.sql` | 第 24 张表 `test_case_results`（task_id/round_no/level/nodeid/name/status/duration_ms/message/origin，CHECK 约束+索引）；表数口径同步 23→24（module_check 契约、test_db、test_two_stage、schema 头注） |
| 解析 | `test_agent/skills/case_results.py`（新） | `parse_junit_xml`（四态+参数化后缀剥除+错误类合成——真 junit 的 message 属性无错误类、type 属性常缺、错误类在 traceback 尾行，受控集成实测后三处合成）；`parse_jest_results`（复用 js_adapter 的 `_extract_trailing_json` 鲁棒提取）；`store_case_results`（round≤1 delete-first 与 evaluation_rounds 同语义；origin=nodeid 匹配生成用例名判 generated/repo）；`execution_snapshot`（纯 SQL 组装：任务态+逐用例+轮次轨迹+日志尾+granularity 诚实标注） |
| 执行链 | `graph/nodes.py` | `build_run_cmd` 加 `--junitxml=/tmp/mta_junit.xml`（jest 无需改——--json stdout 已带逐用例）；run 节点每轮执行后：Python=cat junit 文件解析、JS=转换前抽原始 jest JSON → 落库 → agent_messages 留痕；解析失败/超时无文件=诚实缺席，**不炸执行主链路**（try/except 包裹） |
| API | `api/server.py` | `GET /api/tasks/{tid}/execution`（404/快照） |
| 前端 | `app.js` + `index.html` | 详情页流水线下方新增 `#td-exec`「实时用例执行」面板：最新轮次逐用例表（用例/状态徽章四态/耗时/轮次/来源）+失败行红底+失败消息 details 展开+轮次轨迹行+最近动作日志尾；活跃态 2s 轮询（EXEC_POLL，离开页面/终态即停）；空态两分：活跃任务"尚未执行到用例阶段"、旧任务"没有逐用例结果（本机制上线前运行）"；**粒度诚实标注**「按轮次更新」徽章+说明 |

## C3 验收证据

- **受控测试（不跑 LLM 不碰 Docker）**：`test_受控集成_真pytest输出端到端`——宿主真跑 3 用例（过/败/参数化×2）→ 真 junitxml → 解析（4 条含参数化展开）→ 落库 → 快照；另 7 单测覆盖四态解析/坏 XML 诚实空/jest 逐用例/非 JSON 诚实空/落库 origin 分类与轮次 delete-first/快照组装/API 404。
- **Playwright（12/12 PASS，截图 data/reports/screenshots/taskC/）**：#281 真实空态（诚实文案）；API 拦截 mock 逐用例数据 → 表格渲染/状态徽章/来源列/耗时/轮次轨迹/执行中徽章/日志尾/失败行展开见消息/零控制台错误。
- pytest 全量 **435 passed, 1 skipped**（427+8）；module_check **136 OK**（24 表契约过）；node --check 过。

## 能力边界（如实标注）

- **轮次级粒度**：每轮跑完才出现该轮逐用例结果（容器内 exec 阻塞到轮次结束）；不是逐用例实时流，前端徽章与 API `granularity=round` 双处标注。逐用例流式需 attach 容器日志流+pytest 进度插件，列未来工作。
- 旧任务（机制上线前）无逐用例数据——面板诚实说明，不伪造。
- 超时轮（exit=-1）无 junit 文件——该轮逐用例缺席，轮次汇总仍在（evaluate 侧 stdout 解析）。

## 未做项

- L2/L3/Leg 的逐用例落库（store_case_results 已留 level 参数，接线列后续）。
- SSE 升级（粒度不变前无收益）。
