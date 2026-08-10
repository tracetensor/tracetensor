from pricing import subtotal, tax


def total(items, discount_pct=0):
    base = subtotal(items)
    taxed = base + tax(base)
    if discount_pct:
        return round(taxed * (1 - discount_pct / 100), 2)
    return taxed
