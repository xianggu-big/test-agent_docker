# 修复验证批判定附录（2026-11-18 19:37 批次，接 bugverify_results.json）

驱动器粗判=修复实证 3/6；本附录按 18 格口径逐格定性（证据=agent_messages 评估轨迹+llm_calls）。

| 格 | 族 | 终态 | 定性 | 依据 |
|---|---|---|---|---|
| #1966 | 族D ts-jest diagnostics（nestjs） | failed 补测预算耗尽（3 轮） | ✅**修复实证（执行层打通）**+已知边界重现 | 修复前=ts-jest config-set 崩溃连执行都到不了；本轮 3 轮**真实执行**（每轮 0 过 1 败，失败点=src/app.controller.ts:1 require 链=上午已定性的「被测项目依赖完整性边界」非平台故障） |
| #1967 | 族D ts lib 落后连坐（ts-calc） | failed 疑似产品缺陷（证人保留） | ✅**修复实证（真检出）** | ts lib 修复后用例真跑并抓到埋的缺陷——断言失败证人如实上报，不回炉迁就 |
| #1968 | 族D ts-jest+testMatch 双修（application-template） | failed 同指纹熔断 | ❌**仍失败（待取证）** | 3 轮 0 用例，jest-config readConf（index.js:147）崩溃→repair_env×2→熔断——与 1655 第三层同族但崩点不同（config 解析层非 transform 层），是否新形态待下轮精确取证 |
| #1969 | 族C gson JPMS→go-shop | failed 疑似产品缺陷（证人保留） | ✅**修复实证（真检出）** | go 多包布局修复后用例真跑抓到缺陷 |
| #1970 | 族A java 多模块 jacoco（microservices） | failed 同指纹熔断 | ⚠️**已知边界重现（存疑 35 方向未实施）** | 3 轮 0/0/N-A（surefire 输出解析 0 计数+TOTAL 缺失）→repair_gen 空转→熔断——多模块 jacoco 无单一 TOTAL 行=已拍板方向（命令行注入 jacoco+喂 XML）尚未实施，非新形态 |
| #1971 | 族E laravel 新形态 | failed 疑似产品缺陷（证人保留） | ✅**修复实证（真检出）** | 上轮熔断未知形态，本轮全链路跑通并真检出——链路故障排除 |

**批次结论：修复实证 4/6**（1966 执行层打通+3 格真检出）；已知边界重现 1（1970，方向已拍板待实施）；**待取证 1**（1968 ts-jest config 崩溃，下一轮 Phase0 取证对象）；零新增平台故障形态。

成本：六格 195,241 tokens ≈ ¥0.39（另 E2E 三格 72,108 tokens ≈ ¥0.14，共 9 格 267k ≈ **¥0.53**）。

执行备注：本批 18:01 定时因会话重启连带后台进程死亡未触发；19:05 server 重启后 19:06 补点，首拍 /run 六格全 409（env_gate 在 Docker 未就绪时拦停+自动补齐），45 秒后 Docker 就绪，19:09 人工补发 run 全 200。**驱动器健壮性缺口（run 不检查响应码+无 env_gate 预检）已记录，下轮修**。
