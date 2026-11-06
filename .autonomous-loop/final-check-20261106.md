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
| B3-1 | 黑3UI | Playwright 镜像注册 IMAGE_BUILD_CTX | **未做** | - | - |
| B3-2 | 黑3UI | demo 前端 3 场景真实执行+截图/trace 落库 | **未做** | - | - |
| B3-3 | 黑3UI | 埋按钮失效缺陷被命中出缺陷报告单 | **未做** | - | - |
| B3-TM | 黑3UI | selector 真值闸负例+操作序列 schema+快照 fixture | **未做** | - | - |
| B4-1 | 黑4gRPC | proto demo 清单提取正确 | **未做** | - | - |
| B4-2 | 黑4gRPC | 合法 RPC 通过+虚构 method 被闸剔除留痕 | **未做** | - | - |
| B4-TM | 黑4gRPC | proto 解析正负例+闸门负例+grpcurl 参数拼装 | **未做** | - | - |
| B5-1 | 黑5抓包 | 实施或边界声明（二选一，声明不视为缺口） | **未定** | - | - |
| W1-1 | 白1TS | TS demo 真机全链路（声称 PASS 8/8） | **通过** | 证据文件抽查：8/8 true+轮次 16过3败在案 | - |
| W2-1 | 白2Java | Java demo 全链路+埋缺陷归因 product_defect | **通过** | smoke_java 7/7（data/reports/java_accept/smoke_java_20261107-035437.json），返工 4 轮实锤入卡 | 4 |
| CP1 | ◆检查点一 | TS+Java 两语言真机全链路证据齐+回归全绿 | **通过** | smoke_ts 8/8 + smoke_java 7/7 + 回归 679 全绿（台账检查点一段） | - |
| W3-1 | 白3Go | Go demo 全链路+编译失败→R-Loop token 成本记录进任务卡 | **未做** | - | - |
| W4-1 | 白4PHP | PHP demo 全链路 | **未做** | - | - |
| W5-1 | 白5.NET | .NET demo Tier3 static_scan 兜底表现确认 | **未做** | - | - |
| F1 | 收口 | README 语言支持表按实况更新 | 部分正确（TS 已更新；Java/Go/PHP 未接故维持 Tier3 描述=实况一致） | README.md | - |
| F2 | 收口 | pytest 全绿+module_check 无失败+每语言真机证据链 | 部分（py/js/ts/java 四语言证据链齐；go/php/.NET 待阶段3-5） | data/reports/{ts_accept,java_accept}/ | - |
| AC-1 | 反作弊 | tests/ 目录 git log -p 抽查断言弱化/删除/跳过 | 待查 | - | - |
| AC-2 | 变异抽查 | sqlglot 只读闸/selector 真值闸/gRPC method 闸改坏变红 | 依赖对应阶段实现 | - | - |

## 缺口处理日志
- [缺口0] 前序声称全部完成 → git 史对照仅 2/9 阶段有实现 → 其余 7 阶段按执行序补做到完 → 本矩阵逐条更新
- [缺口1] 反作弊静态核查发现：黑包阶段1 新端点 /api/tasks/{tid}/logs 与既有 api_logs（81d5d86，agent_messages 日志流）同路径双 handler → FastAPI 注册顺序遮蔽老端点（前端无直接消费=影响面零，但语义资产丢失+测试漏改 URL 时 TypeError 实证遮蔽存在）→ 根因=任务卡起草未盘点既有路由 → 修法=新端点改 /app_logs（与表名一致）+测试同步+任务卡偏差入档 → commit → 重验 653 全绿

## 阻塞清单
- （暂无）
