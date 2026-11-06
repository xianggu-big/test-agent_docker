# 开发台账（zcode 闲时开发 · 黑盒能力补齐 + 多语言工具链 两任务包）

## 全局状态
- 当前执行序位置：4. 黑包·阶段3 UI Playwright（执行序 1/2/3+检查点一已完成见阶段条目；终验矩阵=.autonomous-loop/final-check-20261106.md）
- 基线（2026-11-07 03:56 终验轮实测）：pytest 679 passed / 1 skipped；module_check 138 OK / 0 MISS / 0 FINDING
- ◆检查点一（2026-11-07 03:57 达成）：TS（smoke_ts 8/8）+Java（smoke_java 7/7）两语言真机全链路证据齐+回归 679 全绿——白包阶段 1/2 正式收口
- 恢复现场记录（01:35-01:45 本会话）：台账在（执行序1已完成 6db3f26）；工作树遗留=上会话 TS Tier2 半成品（六位置实现+单测+demo+smoke 脚本基本齐、未测试未验收未 commit）；E9/E10 波次未在跑、无 python 进程占线 → 本轮可写；Docker Desktop 已由 preflight 拉起（21s），mta-test:node20-jest 实测含全局 jest 29.7.0/tsc 5.9.3/ts-jest@29
- 统一执行序：1黑包日志 ✅ → 2白包TS ✅ → 3白包Java → ◆检查点一 → 4黑包DB → ◆检查点二 → 5黑包UI → 6白包Go → 7黑包gRPC → 8白包PHP → 9收尾(.NET最小化+抓包声明+README语言表)
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

### 2. 白包·阶段1 TypeScript 升级 Tier2 [已完成（2026-11-07 02:40，验收 PASS 8/8）]
- 子任务进度：S1 调研 [done：容器探针三实验——tsc 闸正负例 OK/全局 ts-jest transformer 实测挂（npm 全局布局嵌套 jest-util 不可达，扩展 NODE_PATH 亦失败）/本地补装是唯一通路且散装形态 npm --no-save 实测可用] / S2 实现 [done：上会话半成品（六位置+单测+demo+smoke）+ 本会话三修——①ts_loose 散装补装（probe_deps_manifest 新形态，不破「真无清单=零执行」契约，_install_ts_tooling 抽函数）②解析层语言形态闸（_parse_cases 加 lang，JS/TS 只认 jest 形态+pytest 槽位模板同剔，refill/cross_review 透传）③_pkg_declares_jest 改 JSON 解析判顶层键（子串被 scripts 值误命中实锤）+collect 闸废案留档 collect_gate_reject/] / S3 测试 [done：653 passed/1 skipped + 守卫 138 OK/0 FINDING] / S4 验收 [done：smoke_ts.py 8/8 PASS 证据 data/reports/ts_accept/smoke_ts_20261107-023706.json]
- 验收记录：任务卡【验收】逐条 → 全过（smoke 8 checks：语言判定/生成 .test.ts/framework 落库/R-Loop 轮次（16过3败）/逐用例落库/覆盖率映射回 .ts（容器臂 coverageMap 键=/work/src/calc.ts 直接证据）/埋的 clamp 缺陷暴露（product_defect 证人）/终态产出）；任务卡验收栏已勾选+S1 调研结论+返工 3 轮+偏差如实入档
- 返工记录：第 1 轮→LLM 混写 pytest 进 .test.ts 穿透解析层旧过滤→tsc 闸整案废案三轮全灭→解析层语言形态闸+禁混写纪律（废案留档立功定位）；第 2 轮→_pkg_declares_jest 子串误命中 "test": "jest"→不注入内联配置→suite 全挂→JSON 解析顶层键；第 3 轮→smoke 判据两处错（nodeid 形态/per_file 走 B2 口径数据源——100% 全覆盖文件行有意不进 per_file 是设计非 bug，test_prune 守卫，误修已撤销）→判据对齐真实形态+verify_sourcemap 零 LLM 容器臂
- 教训沉淀：①「先小样再放量」再次救命——容器探针先于全图跑，全局 ts-jest 挂在烧真机额度前暴露；②上会话注释声称「子串判定即可」被真机证伪——自信注释≠契约，判定逻辑要按最坏输入写测试；③smoke 判据必须对着数据真实形态写（nodeid=fullName 无文件前缀/per_file 是 B2 缺口口径），判据写错会冤枉正确实现；④野进程 & 后台启动占 tmp 库——后台任务一律 run_in_background 受管
- 下次接续入口：阶段 3 Java（S1 调研先行）

### 3. 白包·阶段2 Java 接线 [未开始]
### 4. 黑包·阶段2 DB sidecar+db_assert [未开始]
### 5. 黑包·阶段3 UI Playwright [未开始]
### 6. 白包·阶段3 Go 接入 [未开始]
### 7. 黑包·阶段4 gRPC [未开始]
### 8. 白包·阶段4 PHP 接入 [未开始]
### 9. 收尾 [.NET 最小化/抓包声明/README 语言表/两包验收栏勾选] [未开始]
