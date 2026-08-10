#!/bin/bash
set -euo pipefail
sed -i 's/TAX_RATE = 0.08/TAX_RATE = 0.10/' /app/pricing.py
cat > /app/checkout.py <<'PY'
from pricing import subtotal, tax


def total(items, discount_pct=0):
    base = subtotal(items)
    discounted = round(base * (1 - discount_pct / 100), 2) if discount_pct else base
    return round(discounted + tax(discounted), 2)
PY
