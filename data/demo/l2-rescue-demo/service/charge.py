"""计费服务：金额打折（会员费率见 billing.rate）。"""


def charge(amount):
    return round(amount * _rate_of(amount), 2)


def _rate_of(amount):
    from billing.rate import rate_for
    return rate_for(amount)
