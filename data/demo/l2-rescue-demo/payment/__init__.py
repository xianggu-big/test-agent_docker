"""支付门面（L2 急救真机格构造项目）。

闭包链：payment -> service.charge -> billing.rate（3 模块 2 跳）。
转调必须写在函数体内（AST 调用边只收函数级调用）。"""


def pay(amount):
    from service.charge import charge
    return charge(amount)
