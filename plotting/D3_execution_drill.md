# D3: Execution Quality Across Venues (~30 min)

We route orders to five venues, A to E. The head of execution asks: *"Where do we get filled, what does it cost us, and has anything changed?"*

**Data**: `data/d3_orders.parquet`, 20 days, about 60k orders. Run `python3 make_data.py` once to create it.

| column | meaning |
|---|---|
| `date`, `ts` | day, and order time (ET) |
| `venue` | `A` … `E` |
| `order_size` | shares |
| `filled` | True/False |
| `slippage_bps` | execution cost vs arrival mid, in bps (NaN if the order was not filled) |

For each question: choose a chart and say **why that chart**, make it, then explain what it shows in 2–3 sentences.

1. Which venue fills our orders best?
2. Does order size affect slippage?
3. Is there a time of day when a venue struggles?
4. How has our routing mix across venues changed over the 20 days?

**Present**: which single finding would you take to the head of execution first, and with which chart?
