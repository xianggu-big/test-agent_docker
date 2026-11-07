# grpc-shop demo（黑包阶段4 gRPC 真机验收需求）

一个 gRPC 订单查询服务（proto3 定义见 shop.proto，端口 50051）：

1. `Shop.GetOrder(id)`：按 id 查订单，返回订单详情；不存在的 id 返回 NOT_FOUND；
2. `Shop.ListOrders(status)`：按状态列出订单（status 为空返回全部）。

现有订单：id=1 alice 120.50 paid；id=2 bob 80.00 created；id=3 carol 45.25 paid。

要求对两个 rpc 设计调用用例（含 1 条不存在的 id 预期失败）。
