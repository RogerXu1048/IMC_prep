# D1: Order-Gateway Latency (~30 min)

Our order gateways (`gw-1` to `gw-4`) stamp each order on the way in and on the way out. The engineering manager asks: *"How is gateway latency, and is anything wrong?"*

**Data**: `data/d1_latency.parquet`, one trading day, about 200k orders. Run `python3 make_data.py` once to create it.

| column | meaning |
|---|---|
| `ts` | time the order arrived (ET) |
| `gateway` | `gw-1` … `gw-4` |
| `latency_us` | time spent inside the gateway (µs) |
| `load_msgs_per_ms` | total message load on the box at that moment |

For each question: choose a chart and say **why that chart**, make it, then explain what it shows in 2–3 sentences.

1. What does order latency look like overall?
2. Compare the four gateways. Is any of them behaving differently?
3. Does anything change over the course of the day?
4. Why is latency higher at some times than at others?

**Present**: pick the one chart you'd show the manager, and give it a title that states the takeaway.
