Two input files:

- `/data/orders.csv` — columns `order_id,customer_id,amount`
- `/data/customers.json` — JSON array of `{ "id": int, "name": string }`

Join orders to customers on `customer_id == id`.

Write `/data/merged.json` — a JSON array of objects with **exactly** these keys:

- `order_id` (int)
- `customer_id` (int)
- `customer_name` (string)
- `amount` (int)

Sort the array by `order_id` ascending. Skip orders whose customer id is missing
from the customer list.
