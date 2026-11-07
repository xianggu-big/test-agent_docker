"""grpc-shop demo 服务器（黑包阶段4 真机验收载体）：grpcio 实现 Shop 服务，无 reflection
（grpcurl 走 -proto 模式即可——S1 探针结论）。启动：grpc_tools.protoc 编译 + python server.py。"""
import os
import sys
from concurrent import futures

import grpc

import shop_pb2
import shop_pb2_grpc

ORDERS = {
    1: (1, "alice", 120.50, "paid"),
    2: (2, "bob", 80.00, "created"),
    3: (3, "carol", 45.25, "paid"),
}


class ShopServicer(shop_pb2_grpc.ShopServicer):

    def GetOrder(self, request, context):
        o = ORDERS.get(request.id)
        if o is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"order {request.id} not found")
        return shop_pb2.GetOrderResponse(order=shop_pb2.Order(
            id=o[0], customer=o[1], amount=o[2], status=o[3]))

    def ListOrders(self, request, context):
        rows = [o for o in ORDERS.values()
                if not request.status or o[3] == request.status]
        return shop_pb2.ListOrdersResponse(orders=[
            shop_pb2.Order(id=o[0], customer=o[1], amount=o[2], status=o[3]) for o in rows])


def main() -> None:
    port = int(os.environ.get("GRPC_PORT", "50051"))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    shop_pb2_grpc.add_ShopServicer_to_server(ShopServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    print(f"grpc-shop serving on :{port}", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    sys.exit(main())
