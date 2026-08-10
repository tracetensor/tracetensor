The checkout module in `/app` has **two bugs**:

1. `pricing.py` uses the wrong tax rate (should be **10%**, i.e. `0.10`).
2. `checkout.py` applies the percent discount **after** tax. It must apply the
   discount to the **subtotal first**, then compute tax on the discounted amount.

Fix both files so `checkout.total(items, discount_pct)` returns the correct
totals.

Do not change function signatures.
