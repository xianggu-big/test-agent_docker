# H1 基线真跑：python-titlecase（2026-10-30 夜，任务 159）

- 被测：https://github.com/ppannuto/python-titlecase.git（真实 GitHub URL，代理开启，git clone 正常）
- 需求：标题大小写转换的单元测试（缩写词保护/小词规则/边界/空串），行覆盖率目标 70%
- 链路：API 建任务→确认→下发，9 节点走完，LLM 5 次调用 11,103 token，全程 60s，零人工干预

## 结果

- **28 过 / 6 败，覆盖率 48%**，终态 **failed**（6 个断言失败被归为 product_defect，证人语义如实上报）
- 但本地复跑核验：**6 个失败全部是 oracle 错误（LLM 期望值写错），titlecase 库本身无缺陷**：
  - 期望 `iPad` 被保护 → 真实行为不保护（保留全大写词、普通驼峰不保护）
  - 期望混合大小写按词转换 → 真实行为保留全大写词（视作缩写词）
- **归因结论：假缺陷上报**——product_defect 判定被 6 个错误期望触发，成熟库被诬报"疑似产品缺陷"

## 根因链（与缺陷池对应）

1. **D1 实锤（真跑证据）**：gen_tests 提示词仅 **1,765 token**（≈6KB）——预检预测的 setup.py-only
   成立，10.6KB 库源码没喂 → LLM 纯靠世界知识盲写期望 → 期望与真实行为偏差 → 断言失败
2. 失败 → decide 判 product_defect（断言失败=产品证人）→ **归因无"期望错误"自省**（oracle 问题的
   系统性表现，2412.14137 实证复现）
3. 静态闸放行（D3：包模块盲区），collect-only 也过（import 真实存在）——两道闸只能拦"结构错"，
   拦不住"语义错"（设计如此，但 D1 使语义错成为必然）

## 量表打分（基线）：**7/10 = 70%**

| # | 条目 | 判定 | 证据 |
|---|---|---|---|
| 1 | 终态 done 无 crash | ✗ | failed（假 product_defect 所致，非 crash 但系统原因） |
| 2 | analyze 产物真实 | ✓ | modules: path='.' python loc=762 strategy=full |
| 3 | plan/RTM 产物非空 | ✓ | 15 条测试条件落库；RTM 映射仅 1/15（弱，另记） |
| 4 | 三道闸 | ✓ | 静态闸+collect-only 均过（D3 使闸对包项目宽失效） |
| 5 | 沙箱真实执行 | ✓ | 34 用例容器内真实执行，pass/fail 明细 |
| 6 | 覆盖率 | ✓ | 48%（per_file 含 titlecase/__init__.py 54%） |
| 7 | 归因三分类 | ✗ | 6 个 oracle 错误全归 product_defect（假缺陷上报） |
| 8 | 报告+图表+审查 | ✓ | report/charts/review（rules+llm）=pass |
| 9 | 四级视图可用 | ✗ | API 200 但 trace_count=0、L1 卡缺失（→D6） |
| 10 | 全程免救砖 | ✓ | 零人工干预 |

## 新发现

- **D6【P1·语义】普通（非 levels 流）L1 任务在四级视图为空壳**：/levels 返回 200 但无 L1 卡、
  trace_count=0——test_levels 台账只记录 levels 流任务，直接 L1 任务游离在四级视图外。
- RTM 映射 1/15：map_rtm 的 2-gram 匹配对英文函数名 vs 中文条件的映射能力弱（记录，暂不修——
  跨语言 2-gram 是已知边界，改善属论文 v2 话题）
- 仓库自带测试 titlecase/tests.py 未被执行（pytest 默认不收集 tests.py 命名；且系统 D5 把它当源码）——
  "与仓库自带测试对照"在当前系统里不存在此环节（记录为能力边界）

## 判据走向

基线 70% ≥ 60% → 按用户判据，H3 目标上调为 **≥75%**；H2 修 D1/D2/D3/D5/D6 后重跑同项目验证
（预期：期望正确→假缺陷消失→终态 done→归因干净→9-10/10）。
