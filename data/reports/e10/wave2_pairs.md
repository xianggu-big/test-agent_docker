# Wave2 v2 配对差分（任务4 v2：三表+三层判定+配对键修正）

格数 155（K 124 / D 31）；K 纵向配对组 40；横向 K3-vs-D 配对 3 对（unpaired：K 侧 6 / D 侧 0——D 臂 146 格续跑后补齐）

> 配对键修订留痕：原设计含 baseline_tid——数据实锤恒配 0 对（K r3 有 38 基线、D 臂共享 1 基线、交集 0=采样设计使然），修正为 lang+corpus+requirement（matched-pair 本质=同需求不同处理）。

## 表 A · K vs D 配对差分（matched-pair，K 取最厚 KB 轮）

| 指标 | K 均值 | D 均值 | 配对差均值 | 符号检验 p | 有效 n |
|---|---|---|---|---|---|
| detected 率 | 1.00 | 0.67 | +0.33 | 1.000 | 1/3 |
| passed | 18.0 | 12.4 | +5.6 | 1.000 | 3/3 |
| tokens | 19700 | 17238 | +2461 | 1.000 | 3/3 |

**检验力诚实标注**：当前配对 n=3<30，符号检验最小可达 p=0.500——读数作方向性参考，终表以 146 格补齐后为准。

配对明细：

| lang | corpus | requirement | K det/passed | D det/passed | Δpass |
|---|---|---|---|---|---|
| javascript | demo/js-calc | 验证 src/calc.js 的三��函数：
1. add(a, b)：两数相加；任一参数非数字时抛 TypeError | detected/10 | detected/9.5 | +0.5 |
| python | corpus-003 | # 会议室预约 API 需求（corpus-003）

1. 创建会议室 `POST /rooms`：`name` 长度 | detected/36 | not_detected/17 | +19.0 |
| typescript | demo/ts-calc | # 验收需求（ts-calc，TypeScript 执行链路·白包阶段1）

被测模块 src/calc.ts，三组行为 | detected/8 | detected/10.7 | -2.7 |

## 表 B · KB 收敛曲线（K 纵向，Wilson 95% CI）

| 轮 | n | detected 率 [CI] | passed 均值±σ | tokens 均值±σ |
|---|---|---|---|---|
| r1 | 39 | 0.95 [0.83, 0.99] | 15.9±10.2 | 17644.2±8524.8 |
| r2 | 37 | 1.00 [0.91, 1.00] | 15.8±8.3 | 20392.6±8786.9 |
| r3 | 38 | 0.95 [0.83, 0.99] | 14.7±8.7 | 18860.3±9131.9 |

## 表 C · 注入成本代价（K tokens − D tokens，配对差）

配对差均值 +2461 tokens（中位 -130）；K 侧独有成本=KB 注入的 token 开销实证。

## K 纵向三层判定（每基线）

定性层全一致组：37/40

| 基线 | 轨迹（定性层） | 定量层 passed | 趋势层 Δpass |
|---|---|---|---|
| go/base1126 [go-l3l4-demo] | budget_exhausted → ? → ?（一致(budget_exhausted)） | 0 | -（-） |
| javascript/base1156 [js-calc] | detected → detected → detected（一致(detected)） | 10.0±0.0 | 持平（0） |
| javascript/base1167 [js-calc] | detected → detected → detected（一致(detected)） | 9.7±1.2 | 上升（2） |
| javascript/base1169 [js-calc] | detected → detected → detected（一致(detected)） | 11.0±1.0 | 上升（1） |
| javascript/base1283 [js-calc] | error → ? → detected（分歧(2种)） | 5.0±7.1 | 上升（10） |
| javascript/base1310 [js-calc] | detected → detected → ?（一致(detected)） | 8.5±2.1 | -（-） |
| javascript/base1657 [js-calc] | detected → detected → pass（分歧(2种)） | 13.0±2.6 | 下降（-4） |
| javascript/base1663 [js-calc] | detected → detected → detected（一致(detected)） | 11.3±4.2 | 上升（6） |
| javascript/base1977 [js-calc] | detected → detected → detected（一致(detected)） | 9.0±1.0 | 下降（-1） |
| javascript/base1985 [js-calc] | detected → detected → detected（一致(detected)） | 10.0±1.0 | 上升（1） |
| javascript/base1986 [js-calc] | detected → detected → detected（一致(detected)） | 12.0±3.6 | 下降（-7） |
| javascript/base1987 [js-calc] | detected → detected → detected（一致(detected)） | 10.0±0.0 | 持平（0） |
| javascript/base2247 [js-calc] | detected → detected → detected（一致(detected)） | 12.7±7.2 | 上升（1） |
| javascript/base280 [js-calc] | detected → detected → detected（一致(detected)） | 10.3±1.5 | 下降（-2） |
| python/base154 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 27.7±9.7 | 上升（6） |
| python/base166 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 28.0±5.6 | 上升（7） |
| python/base168 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 16.3±5.9 | 下降（-9） |
| python/base178 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 22.7±11.7 | 下降（-22） |
| python/base179 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 28.0±3.0 | 下降（-3） |
| python/base180 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 21.0±11.3 | 下降（-1） |
| python/base181 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 26.3±7.1 | 下降（-9） |
| python/base182 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 19.0±7.8 | 上升（1） |
| python/base183 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 17.3±10.1 | 上升（1） |
| python/base353 [grid-L2E2-corpus-003] | detected → detected → detected（一致(detected)） | 30.7±8.1 | 下降（-14） |
| python/base354 [grid-L2E3-corpus-003] | detected → detected → detected（一致(detected)） | 26.3±7.0 | 上升（6） |
| python/base355 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 21.7±11.6 | 下降（-19） |
| python/base356 [grid-L2E1-corpus-003] | detected → detected → budget_exhausted（分歧(2种)） | 11.0±10.1 | 下降（-20） |
| python/base357 [e7-corpus-003] | ? → detected → detected（一致(detected)） | 32.5±0.7 | -（-） |
| python/base358 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 24.3±10.7 | 上升（18） |
| python/base362 [e7-corpus-003] | detected → detected → detected（一致(detected)） | 20.0±12.3 | 下降（-19） |
| typescript/base1157 [ts-calc] | detected → detected → detected（一致(detected)） | 9.0±1.0 | 下降（-2） |
| typescript/base1170 [ts-calc] | detected → detected → detected（一致(detected)） | 8.0±1.0 | 上升（2） |
| typescript/base1171 [ts-calc] | detected → detected → detected（一致(detected)） | 9.3±1.2 | 上升（2） |
| typescript/base1656 [ts-calc] | detected → detected → detected（一致(detected)） | 9.3±4.2 | 上升（8） |
| typescript/base1662 [ts-calc] | detected → detected → detected（一致(detected)） | 11.3±3.2 | 下降（-6） |
| typescript/base1967 [ts-calc] | detected → detected → detected（一致(detected)） | 11.0±3.6 | 下降（-5） |
| typescript/base1978 [ts-calc] | detected → detected → detected（一致(detected)） | 11.7±3.2 | 下降（-6） |
| typescript/base1988 [ts-calc] | detected → detected → detected（一致(detected)） | 10.0±2.0 | 下降（-2） |
| typescript/base1989 [ts-calc] | detected → detected → detected（一致(detected)） | 10.3±1.2 | 持平（0） |
| typescript/base1990 [ts-calc] | detected → ? → detected（一致(detected)） | 11.5±2.1 | 下降（-3） |

## unpaired（K 侧无 D 对——如实列出）

- go/demo/go-api-book: # 图书管理 REST 服务（Go/gin）

提供图书的查询、创建与删除接口：
- GET /ping：健康探针，返回 {"message
- javascript/demo/js-calc: 测试计算器除法功能（验收格 G8 占位格：堵住单 worker 让竞态格停在 queued）
- javascript/demo/js-calc: 测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。
- javascript/demo/js-calc: 验证 src/calc.js 的三个函数：
1. add(a, b)：两数相加；任一参数非数字时抛 TypeError。
2. div(a,
- python/corpus-003: 对被测项目做功能测试：接口契约与业务流程（添加/查询/取消，边界与非法输入），顺序场景约束（创建→按 id 读回）；行覆盖率目标 70%。
- typescript/demo/ts-calc: 测试项目的核心功能：正常输入、边界值、非法输入与错误处理，覆盖主要接口/函数。
