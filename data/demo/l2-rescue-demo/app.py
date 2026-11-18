"""根级入口（不在任何模块内——L2 格的 scope 跳过台账证据）。"""


def main():
    from payment import pay
    print(pay(120))
