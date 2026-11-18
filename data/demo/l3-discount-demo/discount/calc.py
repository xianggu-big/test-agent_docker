"""折扣计算（L3 真机格构造项目）：VIP 八折，满 200 再减 20。"""


def final_price(price, level):
    p = price * (0.8 if level == "VIP" else 1.0)
    return p - 20 if p >= 200 else p
