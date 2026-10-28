# E4 变异标定：归因器混淆矩阵（classify_failure）

- 生成时间：2026-10-28T08:05:26
- 真值构造：8 个 mutant（product）+ 5 类已知坏用例（case）+ 卸载 pytest（env）
- 受控：不调 LLM（归因为确定性启发式）；每样本独立干净容器；真实 Docker 执行
- 诚实边界：已知真值集上的准确率（标定上界），不等于野外准确率（oracle 问题）

| 样本 | 真值 | 归因预测 | R-Loop 判定 | 预期判定 | 结果 |
|---|---|---|---|---|---|
| M1·条件取反 | product | product | stop | stop | ✅ |
| M2·运算符替换 | product | product | stop | stop | ✅ |
| M3·常量偏移 | product | product | stop | stop | ✅ |
| M4·舍入移除 | product | product | stop | stop | ✅ |
| M5·异常吞掉 | product | product | stop | stop | ✅ |
| M6·初值污染 | product | product | stop | stop | ✅ |
| M7·累加破坏 | product | product | stop | stop | ✅ |
| M8·求和偏移 | product | product | stop | stop | ✅ |
| C1·ImportError坏用例 | case | case | repair_gen | repair_gen | ✅ |
| C2·AttributeError坏用例 | case | case | repair_gen | repair_gen | ✅ |
| C3·SyntaxError坏用例 | case | case | repair_gen | repair_gen | ✅ |
| C4·NameError坏用例 | case | case | repair_gen | repair_gen | ✅ |
| C5·空套件(no tests ran) | case | case | repair_gen | repair_gen | ✅ |
| V1·pytest缺失(env) | env | env | repair_env | repair_env | ✅ |

## 混淆矩阵（行=真值，列=预测）

| 真值\预测 | product | case | env | 无失败 |
|---|---|---|---|---|
| **product** | 8 | 0 | 0 | 0 |
| **case** | 0 | 5 | 0 | 0 |
| **env** | 0 | 0 | 1 | 0 |

- **总体准确率**：14/14 = 100%
- **漏报（真 product 未判 product → 该停没停，造假风险）**：0
- **误报（非 product 判成 product → 不该停停了，误停成本）**：0
- **判定错误样本**：0 条
- 各类召回率：product 100% / case 100% / env 100%

## 论文口径（§7.3）

- 强信号类（ImportError/AttributeError/SyntaxError/NameError/no tests ran）→ case 全对：
  归因器对"用例自身坏掉"的可判别信号是可靠的；
- 断言失败 mutant → product 全对：预期值来自规格（docstring/常识）的用例失败，
  归因器将其判为疑似缺陷并触发证人冻结停机（防造假语义的机械化前提成立）；
- 该矩阵证明的是"在已知真值集上归因正确"，真实失败常信号混合（断言失败也可能是
  用例预期写错），因此系统仍保留人工标注通道与"宁可误报不可造假"的保守口径。
