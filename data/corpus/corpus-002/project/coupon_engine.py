def validate_coupon(coupon, order_total):
    if coupon["expires_in_days"] <= 0:
        return False
    if order_total < coupon["min_total"]:
        return False
    if coupon["type"] == "fixed":
        if coupon["value"] <= 0:
            return False
    elif coupon["type"] == "percent":
        if coupon["value"] <= 0 or coupon["value"] > 100:
            return False
    return True


def calculate_discount(coupon, order_total):
    if coupon["type"] == "fixed":
        discount = coupon["value"]
    else:
        discount = int(order_total * coupon["value"] / 100)
    if discount > order_total:
        discount = order_total
    return discount


def apply_coupon(coupon, order_total):
    if not validate_coupon(coupon, order_total):
        raise ValueError("coupon not applicable")
    discount = calculate_discount(coupon, order_total)
    return order_total - discount


def best_coupon(coupons, order_total):
    best = None
    best_discount = -1
    for coupon in coupons:
        if validate_coupon(coupon, order_total):
            discount = calculate_discount(coupon, order_total)
            if discount > best_discount:
                best_discount = discount
                best = coupon
    return best


def format_amount(cents):
    if cents < 0:
        raise ValueError("amount must be non-negative")
    return f"{cents // 100}.{cents % 100:02d}"
