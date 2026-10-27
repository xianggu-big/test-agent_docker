def apply_discount(price, percent):
    """按百分比打折：percent 取 0~100，返回折后价（四舍五入到两位小数）。"""
    if not 0 <= percent <= 100:
        raise ValueError("percent out of range")
    return round(price * (1 - percent / 100), 2)


class Cart:
    """购物车：add 累加商品，total 返回合计。"""

    def __init__(self):
        self.items = []

    def add(self, name, price):
        self.items.append((name, price))

    def total(self):
        return round(sum(p for _, p in self.items), 2)
