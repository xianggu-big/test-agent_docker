def discount(price, vip):
    """VIP 客户享 8 折优惠，普通客户按原价支付，不叠加任何折扣。"""
    if vip:
        return price * 0.8
    return price * 0.9  # 种植的真实缺陷：普通客户被错误打了 9 折
