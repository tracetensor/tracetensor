TAX_RATE = 0.08


def subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def tax(amount):
    return round(amount * TAX_RATE, 2)
