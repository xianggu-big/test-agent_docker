# 开发台账（zcode 闲时开发 · 黑盒能力补齐 + 多语言工具链 两任务包）

## 全局状态
- 当前执行序位置：1. 黑包·阶段1 运行时日志分流+log_analyze（进行中）
- 基线（2026-11-06 19:07 本会话实测）：pytest 605 passed / 1 skipped；module_check 138 OK / 0 MISS / 探针 25 PASS / 0 FAIL / 0 FINDING
- 恢复现场记录（19:05-19:15）：台账原不存在→按模板创建；工作树不干净→查明=E9/E10 会话已完成未提交的 XBreadth+E10 基建（其 wave_ledger 14:45 条目自证回归 605 绿），本会话基线复验绿后代提交固化（6b47bf0 代码 / 77a8eb8 证据 / 8e1579f 任务包文档）；E10 波次 18:10 已 plan_exhausted 收工（a2 六格 gbk 解码 error 属 E9/E10 会话领域，未动）；当前无波次在跑，仅 uvicorn API（PID 35956，不杀）→ 本轮可写
- 统一执行序：1黑包日志 → 2白包TS → 3白包Java → ◆检查点一 → 4黑包DB → ◆检查点二 → 5黑包UI → 6白包Go → 7黑包gRPC → 8白包PHP → 9收尾(.NET最小化+抓包声明+README语言表)
- 任务包：docs/黑盒能力补齐任务包-20261106.md ｜ docs/多语言工具链接入任务包-20261106.md

## 阶段条目

### 1. 黑包·阶段1 运行时日志分流+log_analyze [已完成（2026-11-06 19:35）]
- 子任务进度：S1 调研 [done：纯工程活无重型依赖，pytest caplog 思路=在源头（容器重定向）捕获并结构化、与测试报告解析分账；四语言格式=排版事实正则非经验规则] / S2 失败测试 [done：tests/test_app_logs.py 22红 + test_app_logs_api.py，其中闸门负例（引用不存在行号）优先] / S3 实现 [done] / S4 测试三连 [done：630 过 1 跳 + 守卫 138 OK/0 FINDING] / S5 真机验收 [done：5/5 PASS]
- 交付物四件全齐：①runner 分流=l2._start_service 重定向 /work/logs/app.log + runner.read_app_log_pair 采集（docker logs 兜底）+ run 节点/provision 失败路径 collect_and_store_app_logs 接线 ②app_logs 第28表（schema.sql 单一真源+表数契约 27→28 同步 test_db/module_check/README/test_two_stage）③log_analyze skill（builtin.py 注册 risk=low/timeout150s/arg_schema，make_deps 全量注册自动覆盖；引用闸 validate_citations 只认「行 N」格式防 L1/L2 级别名误杀）④API GET /api/tasks/{tid}/logs?round=N（鉴权+404+轮过滤，纯 SQL）
- 测试矩阵 24 条全绿：四语言正则各1正1负（8条）+引用闸（不存在行号999拒/真实行过/零引用拒）+路径拼接3条+幂等2条（同轮重存不重复/round1重存清历史）+表契约+skill注册+API 4条
- 验收记录：python scripts/smoke_applogs.py 真机两臂 PASS——A臂（/boom 埋错）app_logs 3行（error_log/python_traceback/python_exception）+快照65行+log_analyze 真LLM引用行7/8/64 citations_ok=True；B臂（端口占用）Errno 98 bind 错误被正则命中；证据=data/reports/applogs_accept/accept_20261106-193425.json；任务卡验收栏已勾选
- 返工记录：第1轮→_collect_app_logs 误用未定义 runner（应为 deps.runner）→修复后三连全绿+验收 PASS
- 实现偏差（已写进任务卡）：容器路径 /work/logs/app.log（非任务卡的 /workspace，遵仓库 /work 约定）；服务启动失败路径也分流（超任务卡范围的环境证据增强）
- 备注：运行中的 uvicorn（PID 35956）是旧模块，/logs 端点要重启服务才可见（不杀进程，用户侧重启）；smoke_applogs.py 全程 tmp 库 data/tmp_applogs_accept/，未碰真实库

### 2. 白包·阶段1 TypeScript 升级 Tier2 [未开始]
### 3. 白包·阶段2 Java 接线 [未开始]
### 4. 黑包·阶段2 DB sidecar+db_assert [未开始]
### 5. 黑包·阶段3 UI Playwright [未开始]
### 6. 白包·阶段3 Go 接入 [未开始]
### 7. 黑包·阶段4 gRPC [未开始]
### 8. 白包·阶段4 PHP 接入 [未开始]
### 9. 收尾 [.NET 最小化/抓包声明/README 语言表/两包验收栏勾选] [未开始]
