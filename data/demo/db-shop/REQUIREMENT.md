# 验收需求（db-shop，DB sidecar + db_assert·黑包阶段2）

被测数据：shop 库 orders 表（5 行种子：3 created / 2 paid / 1 cancelled）。

黑盒 DB 验证关注：
1. 下单行为应使 orders 表新增一行（status='created'）
2. 支付行为应将对应行 status 更新为 'paid'
3. cancelled 行不计入营业统计（SELECT COUNT ... WHERE status='paid' = 2）
