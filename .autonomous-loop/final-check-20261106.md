# 两任务包终验矩阵（final-check-20261106，终验工程师建立于 2026-11-07 03:05）

## 基线：pytest 653 passed / 1 skipped + module_check 138 OK / 0 MISS / 0 FINDING（2026-11-07 02:46 实测）
## 首次对照结论（git log × 两包阶段清单）
- **9 个阶段只有 2 个有对应 commit**（黑包阶段1=6db3f26 / 白包阶段1=168d4a5）
- 黑包阶段 2/3/4/5 与 白包阶段 2/3/4/5 **全部无实现痕迹**——"前序声称已全部完成"**不成立**，
  实际状态=2/9 完成 + 1 个可选/降级项未定。终验=复验已完成 2 条 + 反作弊核查 + 其余按执行序补做到完。

## 矩阵
| # | 阶段 | 验收条目 | 结论 | 证据路径 | 返工轮次 |
|---|------|----------|------|----------|----------|
| B1-1 | 黑1日志 | 真机 L2 冒烟 app_logs 按轮次分档（声称✅） | **通过** | 证据文件抽查：A臂 3行入库+快照65行+citations_ok=True | - |
| B1-2 | 黑1日志 | 端口占用被正则命中+log_analyze 引用真实行号（声称✅） | **通过** | B_port_conflict 臂 port_conflict_hit=true/Errno 98 在案 | - |
| B1-TM | 黑1日志 | 测试矩阵：四语言正则/引用闸/幂等 24 条 | **通过** | test_app_logs.py 279行+test_app_logs_api.py 随全量绿 | - |
| B2-R | 黑2DB | Testcontainers 前置调研结论写回任务卡 | **通过** | 任务卡前置调研段（四要素→抄模式自实现）+Java S1（surefire/JaCoCo/mvn 语义） | - |
| B2-1 | 黑2DB | MySQL demo sidecar 建库成功 | **通过** | smoke_db_assert 9/9（证据 data/reports/db_assert_accept/smoke_db_20261107-031222.json） | 1 |
| B2-2 | 黑2DB | db_assert 只读 SQL 通过/失败各一例 | **通过** | 同上（paid=2 通过例+表不存在失败例，SkillRegistry 通道） | 1 |
| B2-3 | 黑2DB | 越权写 SQL 被闸剔除留痕 | **通过** | 同上（UPDATE rejected+注入整案+orders 表完好机器实证） | 1 |
| B2-TM | 黑2DB | sqlglot 只读闸正负例矩阵+sidecar 配置解析+落库幂等 | **通过** | tests/test_db_assert.py 14 + test_db_sidecar.py 10 全绿 | 1 |
| B3-1 | 黑3UI | Playwright 镜像注册 IMAGE_BUILD_CTX | **通过** | mta-test:playwright build_images [OK]+快照采集（data/reports/ui_assert_accept/evidence_*/dom_snapshot.json=UI 版端点清单）；commit ddf9dd2 | 7 |
| B3-2 | 黑3UI | demo 前端 3 场景真实执行+截图/trace 落库 | **通过** | smoke_ui LLM 生成 3 场景全过闸+2过1败+逐步 png+每场景 trace.zip 实物落盘 | 7 |
| B3-3 | 黑3UI | 埋按钮失效缺陷被命中出缺陷报告单 | **通过** | 删除场景 expect_hidden 失败=按钮仍可见（缺陷精确命中）+缺陷报告单 md 含 trace 路径/失败步骤 | 7 |
| B3-TM | 黑3UI | selector 真值闸负例+操作序列 schema+快照 fixture | **通过** | tests/test_ui_assert.py 25 条（真值闸正负例/派生放行/整案语义/schema 负例矩阵/渲染注入防护/junit 映射/幂等/缺陷单/注册） | 7 |
| CP2 | ◆检查点二 | 归因准确率复验 | **通过** | verify_gates_real.py E1/E2/E3 全 PASS（data/reports/gate_verify/run_20261107-180633/）+新语言帧归属 4 测试永久锁（tests/test_checkpoint2_attribution_replay.py）；commit 664238b | - |
| B4-1 | 黑4gRPC | proto demo 清单提取正确 | **通过** | endpoint_extract D 链第④级 .proto 源+Shop.GetOrder/ListOrders+字段（smoke_grpc 全检查 PASS）；commit c49ed81 | 3 |
| B4-2 | 黑4gRPC | 合法 RPC 通过+虚构 method 被闸剔除留痕 | **通过** | 跨容器 grpcurl 真实调用 passed≥2+Shop.DeleteOrder 整条剔除 rejected_reason 留痕+NOT_FOUND 失败例如实 | 3 |
| B4-TM | 黑4gRPC | proto 解析正负例+闸门负例+grpcurl 参数拼装 | **通过** | tests/test_grpc_assert.py 16 条（解析正负例/D 链+spec 优先/闸正负例×5/拼装/符号归一/幂等/注册/LLM mock×2） | 3 |
| B5-1 | 黑5抓包 | 实施或边界声明（二选一，声明不视为缺口） | **通过（降级声明）** | 任务卡边界声明段（mitmproxy 侵入性/语料无场景/明文允许降级） | - |
| W1-1 | 白1TS | TS demo 真机全链路（声称 PASS 8/8） | **通过** | 证据文件抽查：8/8 true+轮次 16过3败在案 | - |
| W2-1 | 白2Java | Java demo 全链路+埋缺陷归因 product_defect | **通过** | smoke_java 7/7（data/reports/java_accept/smoke_java_20261107-035437.json），返工 4 轮实锤入卡 | 4 |
| CP1 | ◆检查点一 | TS+Java 两语言真机全链路证据齐+回归全绿 | **通过** | smoke_ts 8/8 + smoke_java 7/7 + 回归 679 全绿（台账检查点一段） | - |
| W3-1 | 白3Go | Go demo 全链路+编译失败→R-Loop token 成本记录进任务卡 | **通过** | smoke_go 双臂 PASS 7/7（GetTier(90) 缺陷 90 端点用例精确命中=product_defect 证人+覆盖率 1.0）；**Arm B 论文数据=编译失败→重生成循环单元成本≈10.5k tokens**（5712+4983，闸真实编译失败复现+重生成闸过）；commit 7cd689e | 5 |
| W3-TM | 白3Go | Go 六位置测试矩阵 | **通过** | tests/test_go_pipeline.py 19 条（形态闸正负例/命名/闸命令/包壳/adapter 事件流+编译失败+panic 段/coverprofile B2/归因 panic 帧归属×4/命令/tier/canary） | 5 |
| W4-1 | 白4PHP | PHP demo 全链路 | **通过** | smoke_php PASS 7/7（composer 探测→install_deps composer 分支→php -l 闸→phpunit→junit 复用解析→GetTier(90) 缺陷暴露=product_defect 证人，轮次 10过3败）；commit 50fa3ca | 5 |
| W4-TM | 白4PHP | PHP 六位置测试矩阵 | **通过** | tests/test_php_pipeline.py 12 条（形态闸/命名/闸命令/manifest 探测/adapter+clover B2/归因分支/命令/tier/canary） | 5 |
| W5-1 | 白5.NET | .NET demo Tier3 static_scan 兜底表现确认 | **通过** | dotnet-notes demo：csharp 识别+空 catch 跨行命中（修复 2 枚既有漏报）；单测 4 条；任务卡验收栏 | - |
| F1 | 收口 | README 语言支持表按实况更新 | **通过** | README.md：TS/Java/Go/PHP 四 Tier2 行+黑盒五通道行+诚实边界更新（2026-11-07 实况） | - |
| F2 | 收口 | pytest 全绿+module_check 无失败+每语言真机证据链 | **通过** | pytest 762 passed/1 skipped+module_check 138 OK/0 FINDING（2026-11-07 20:45 实测）；六语言证据链=data/reports/{ts_accept,java_accept,go_accept,php_accept,ui_assert_accept,grpc_assert_accept,db_assert_accept,applogs_accept}/ | - |
| AC-1 | 反作弊 | tests/ 目录 git log -p 抽查断言弱化/删除/跳过 | **通过** | git log -p 抽查 tests/（UI/Go/gRPC/PHP 四新增测试文件+契约更新 test_multilang/test_ts_tier2/test_recipe/test_db 等）——无断言弱化/删除；契约测试更新均为合法语义变更（tier 集合/表数），断言本体未弱化 | - |
| AC-2 | 变异抽查 | sqlglot 只读闸/selector 真值闸/gRPC method 闸改坏变红 | **通过** | 三闸变异抽查实测（2026-11-07 收口轮终验段：临时改坏→负例变红→还原→全绿，见终验报告②） | - |

## 缺口处理日志
- [缺口0] 前序声称全部完成 → git 史对照仅 2/9 阶段有实现 → 其余 7 阶段按执行序补做到完 → 本矩阵逐条更新
- [缺口2]（2026-11-07 收口轮）执行序 5-9 全部补做到完（黑3 UI/检查点二/白3 Go/黑4 gRPC/白4 PHP/收尾）——每阶段走完整 S1-S6+负例先行+真机验收闭环，矩阵逐条回写
- [缺口1] 反作弊静态核查发现：黑包阶段1 新端点 /api/tasks/{tid}/logs 与既有 api_logs（81d5d86，agent_messages 日志流）同路径双 handler → FastAPI 注册顺序遮蔽老端点（前端无直接消费=影响面零，但语义资产丢失+测试漏改 URL 时 TypeError 实证遮蔽存在）→ 根因=任务卡起草未盘点既有路由 → 修法=新端点改 /app_logs（与表名一致）+测试同步+任务卡偏差入档 → commit → 重验 653 全绿

## 阻塞清单
- （暂无）
