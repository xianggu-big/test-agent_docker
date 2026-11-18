"""购物车聚合。"""


def total(items):
    return sum(i["price"] for i in items)
