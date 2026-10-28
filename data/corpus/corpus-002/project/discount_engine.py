def validate_coupon(coupon, subtotal_cents):
    if coupon is None:
        return False, "no_coupon"
    if coupon.get("expired"):
        return False, "expired"
    if subtotal_cents < coupon.get("min_spend", 0):
        return False, "below_min_spend"
    ctype = coupon.get("type")
    if ctype not in ("fixed", "percent"):
        return False, "invalid_type"
    if ctype == "percent":
        value = coupon.get("value", 0)
        if value < 1 or value > 100:
            return False, "invalid_percent"
    return True, "ok"


def calculate_discount(coupon, subtotal_cents):
    ctype = coupon.get("type")
    if ctype == "fixed":
        value = coupon.get("value", 0)
        return min(value, subtotal_cents)
    if ctype == "percent":
        value = coupon.get("value", 0)
        raw = subtotal_cents * (100 - value) // 100
        max_discount = coupon.get("max_discount")
        if max_discount is not None and raw >= max_discount:
            return max_discount
        return max(raw, 0)
    return 0


def apply_coupon(coupon, subtotal_cents):
    ok, _reason = validate_coupon(coupon, subtotal_cents)
    if not ok:
        return subtotal_cents, 0
    discount = calculate_discount(coupon, subtotal_cents)
    payable = subtotal_cents - discount
    return payable, discount


def settle_order(items, coupon=None):
    subtotal = 0
    for item in items:
        subtotal += item.get("price_cents", 0) * item.get("qty", 0)
    if not items:
        return 0, 0, 0, "empty_order"
    for item in items:
        if item.get("qty", 0) <= 0:
            return subtotal, 0, subtotal, "invalid_quantity"
    if coupon is None:
        return subtotal, 0, subtotal, "ok"
    payable, discount = apply_coupon(coupon, subtotal)
    return subtotal, discount, payable, "ok"