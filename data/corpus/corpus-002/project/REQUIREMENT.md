# 优惠券与订单折扣引擎需求文档

## 1. 概述

本模块提供优惠券校验、折扣计算与订单结算功能。所有金额以「分」为单位，使用整数运算，避免浮点误差。模块对外暴露以下函数：

- `validate_coupon(coupon, subtotal_cents)`
- `calculate_discount(coupon, subtotal_cents)`
- `apply_coupon(coupon, subtotal_cents)`
- `settle_order(items, coupon=None)`

## 2. 数据结构

优惠券用 dict 表示，字段如下：

- `code` (str)：券码
- `type` (str)：`"fixed"`（满减）或 `"percent"`（折扣）
- `value` (int)：满减为减免金额（分）；折扣为百分比整数，如 85 表示 85 折
- `min_spend` (int)：最低消费门槛（分）
- `max_discount` (int 或 None)：折扣券最高减免金额（分），满减券忽略
- `expired` (bool)：是否过期
- `stackable` (bool)：是否可与其他券叠加（本模块单券场景，仅做校验）

订单项用 dict 表示：`{"name": str, "price_cents": int, "qty": int}`。

## 3. 函数行为契约

### 3.1 validate_coupon(coupon, subtotal_cents)

返回 `(ok: bool, reason: str)`。校验顺序与规则：

1. 若 `coupon` 为 `None`，返回 `(False, "no_coupon")`。
2. 若 `coupon["expired"]` 为真，返回 `(False, "expired")`。
3. 若 `subtotal_cents < coupon["min_spend"]`，返回 `(False, "below_min_spend")`。**边界：当 `subtotal_cents` 恰好等于 `min_spend` 时视为满足门槛，应通过此校验。**
4. 若 `coupon["type"]` 不是 `"fixed"` 或 `"percent"`，返回 `(False, "invalid_type")`。
5. 若 `coupon["type"] == "percent"` 且 `value` 不在 1~100 之间（含端点），返回 `(False, "invalid_percent")`。
6. 其余返回 `(True, "ok")`。

### 3.2 calculate_discount(coupon, subtotal_cents)

返回 `int`，表示该券在给定小计下可减免的金额（分）。**调用方保证已通过 validate_coupon**。计算规则：

- 满减券：减免金额 = `min(value, subtotal_cents)`，即减免不超过小计。
- 折扣券：先算 `raw = subtotal_cents * (100 - value) // 100`（向下取整）。若 `max_discount` 不为 `None` 且 `raw > max_discount`，则减免 `max_discount`；否则减免 `raw`。
- 任何情况下减免金额不得为负。

### 3.3 apply_coupon(coupon, subtotal_cents)

返回 `(payable_cents: int, discount_cents: int)`。先调用 `validate_coupon`：

- 若校验失败，`discount_cents = 0`，`payable_cents = subtotal_cents`。
- 若校验通过，`discount_cents = calculate_discount(coupon, subtotal_cents)`，`payable_cents = subtotal_cents - discount_cents`。

**边界：`payable_cents` 不得小于 0。**

### 3.4 settle_order(items, coupon=None)

1. 计算小计：`subtotal = sum(price_cents * qty for each item)`。
2. 若 `items` 为空列表，返回 `(0, 0, 0, "empty_order")`，依次为 `(subtotal, discount, payable, status)`。
3. 若某项 `qty <= 0`，视为非法订单，返回 `(subtotal, 0, subtotal, "invalid_quantity")`。
   - **注意：应先按原样累加所有项得到 subtotal，再检查 qty；qty 为 0 的项对 subtotal 贡献为 0。**
4. 若 `coupon` 为 `None`，返回 `(subtotal, 0, subtotal, "ok")`。
5. 否则调用 `apply_coupon(coupon, subtotal)`，返回 `(subtotal, discount, payable, "ok")`。

## 4. 边界与异常声明

- 所有金额参数假定为非负整数；模块不负责校验负金额。
- `validate_coupon` 对 `coupon` 为 `None` 有明确处理；其他字段缺失不属于本模块契约范围。
- 折扣券 `value` 为 100 时减免为 0，属合法输入。
- 满减券 `value` 大于小计时，减免封顶为小计。
- 空订单与非法数量为两种不同状态，需区分。