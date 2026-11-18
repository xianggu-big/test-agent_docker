def checkout(items, level):
    from discount import final_price
    from cart.cart import total
    return final_price(total(items), level)
