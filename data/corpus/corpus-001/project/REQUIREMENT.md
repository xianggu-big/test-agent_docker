# 密码强度校验器需求文档

## 模块概述

本模块提供密码强度校验相关功能，包含以下公开函数：

- `check_length(password: str, min_len: int = 8, max_len: int = 64) -> bool`
- `has_uppercase(password: str) -> bool`
- `has_lowercase(password: str) -> bool`
- `has_digit(password: str) -> bool`
- `has_special(password: str) -> bool`
- `strength_score(password: str) -> int`
- `is_strong(password: str, min_len: int = 8, max_len: int = 64) -> bool`

## 行为契约

### check_length

校验密码长度是否在 `[min_len, max_len]` 闭区间内。若密码长度小于 `min_len` 或大于 `max_len`，返回 `False`；否则返回 `True`。

### has_uppercase

当且仅当密码中至少包含一个大写英文字母（A-Z）时返回 `True`，否则返回 `False`。

### has_lowercase

当且仅当密码中至少包含一个小写英文字母（a-z）时返回 `True`，否则返回 `False`。

### has_digit

当且仅当密码中至少包含一个数字字符（0-9）时返回 `True`，否则返回 `False`。

### has_special

当且仅当密码中至少包含一个非字母且非数字的可打印 ASCII 字符（即 ASCII 码在 33-126 之间且不属于 A-Z、a-z、0-9 的字符）时返回 `True`，否则返回 `False`。空格字符（ASCII 32）不计为特殊字符。

### strength_score

计算密码强度得分，规则如下（每满足一项加 1 分）：

1. 长度在 `[8, 64]` 闭区间内；
2. 包含至少一个大写字母；
3. 包含至少一个小写字母；
4. 包含至少一个数字；
5. 包含至少一个特殊字符（定义同上）。

返回值为 0 到 5 的整数。

### is_strong

综合判断密码是否为强密码。当且仅当以下所有条件同时满足时返回 `True`：

- 长度在 `[min_len, max_len]` 闭区间内；
- `has_uppercase` 为 `True`；
- `has_lowercase` 为 `True`；
- `has_digit` 为 `True`；
- `has_special` 为 `True`。

否则返回 `False`。

## 边界声明

- 空字符串：`check_length` 在默认参数下返回 `False`；各字符类别检测函数均返回 `False`；`strength_score` 返回 0；`is_strong` 返回 `False`。
- 长度恰好等于 `min_len` 或 `max_len` 时，`check_length` 应返回 `True`。
- 特殊字符检测中，空格不算特殊字符。
- 所有函数对非字符串输入不做特殊处理（即不承诺不抛异常）。
