# F4 — IOC Hit Rate vs Latency: What Are 200 ns Worth?

**Time box: ~2 hours** · Python, pandas, numpy, matplotlib · the interviewer will help with syntax

## Context
We trade US equity options on five exchanges (EX1–EX5). Two liquidity-taking strategies, **S1** and **S2**, send **IOC** orders to take mispriced resting quotes. Every attempt is a **race**: other firms see the same quote, and whoever reaches the matching engine first gets the fill. Sometimes the quote is also cancelled before anyone arrives (a "fade"). The latency team wants to know where we win, how much speed matters, and what an engineering project that makes us **200 ns faster** would be worth.

> "Hit rate" is ambiguous. It can mean take-fill rate, a market maker's quote hit rate, or our share of OCC-cleared volume (OCC = Options Clearing Corporation). In this problem it means **the fraction of our IOC take attempts that receive a fill**. In an interview, clarify the definition first.

## Data
Run `python3 make_data.py`. It writes `../data/p05/take_attempts.parquet`, about 177k rows covering 20 trading days from 2026-10-19.

| column | type | meaning |
|---|---|---|
| `attempt_id` | int64 | attempt id |
| `date` | datetime | trading date |
| `ts_ns` | int64 | UTC epoch ns when the opportunity was detected |
| `venue` | str | `EX1` … `EX5` |
| `strategy` | str | `S1` or `S2` |
| `underlying` | str | underlying ticker |
| `edge_ticks` | int64 | theoretical edge of the opportunity, in ticks |
| `spread_ticks` | int64 | bid-ask spread at detection, in ticks |
| `target_qty` | int64 | displayed size we tried to take (contracts) |
| `reaction_ns` | Int64 | our reaction time, measured from the signal at our NIC to our order leaving the NIC (hardware timestamps) |
| `resp_ns` | int64 | time from our order leaving the NIC to the exchange's response |
| `filled_qty` | int64 | contracts filled |
| `outcome` | str | `FILLED` / `PARTIAL` / `MISSED` |

A tick is $0.01 and the contract multiplier is 100, so 1 tick of edge on 1 contract is worth $1.

## Steps

**Step 1: Load, clean, define (≈20 min).**
Check data quality and decide what to do with each problem you find. Define the hit rate, both count-based and quantity-based. Produce a table by venue and by strategy with a 95% confidence interval.
*Deliverable:* a data-quality summary and the hit-rate tables.

**Step 2: Is the data stable over time? (≈20 min)**
Plot the daily hit rate per venue. Write a simple test that flags day-over-day changes too large to be noise.
*Deliverable:* one plot and a list of flagged (day, venue) breaks, with your interpretation.

**Step 3: The race curve (≈25 min).**
For each venue, show how the hit rate depends on our reaction time. Bin the attempts and give each bin a CI. Choose the time window deliberately.
*Deliverable:* one plot and a short description of how the curves differ across venues.

**Step 4: S1 vs S2 (≈20 min).**
A trader says, "S2 is our better strategy — its hit rate is higher." Check this claim. Then produce one number that compares the two strategies fairly.
*Deliverable:* the comparison and a one-sentence explanation.

**Step 5: What are 200 ns worth? (≈20 min).**
Estimate the extra fills per day and the extra theoretical $ per day if every attempt were 200 ns faster. State each of your assumptions.
*Deliverable:* a per-venue table and a total.

## Discussion / follow-ups
1. Why Wilson rather than `p ± 1.96·sqrt(p(1−p)/n)`? Are the attempts independent, and what does that do to your CIs?
2. Your race curve mixes strategies and edge sizes. How could `edge_ticks` confound the latency effect?
3. Can you infer the competitors' latency distribution from our own hit-rate curve? What assumptions does that need?
4. How would you measure the value of latency with an **experiment** instead of observational data?
5. The hit rate goes up but P&L goes down. How can that happen?
6. How would you monitor this live, so that a sudden change on one venue is caught the same day?
7. If the definition were the **market-maker quote hit rate** or **share of OCC-cleared volume**, what would change in your analysis?
