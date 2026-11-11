# php-shop demo（白包阶段4 PHP 真机验收需求）

一个订单分级模块（PHP 类 DemoShop\Shop，composer classmap autoload）：

1. `Shop::getTier(score)`：按积分分级——**>=90 返回 "A"，>=60 返回 "B"，其余返回 "C"**；
2. `Shop::add(a, b)`：返回两数之和。

要求对两个方法设计测试用例，重点覆盖 getTier 的边界值（90/89/60/59 端点本身）。
