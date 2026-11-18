"""费率表：满 100 打九折。"""


def rate_for(amount):
    return 0.9 if amount >= 100 else 1.0
