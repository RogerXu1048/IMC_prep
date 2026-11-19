# F4: IOC Take-Attempt Hit Rate vs Our Latency

**Time box: ~2 hours (aim for ~100 lines of core code)** · Python, pandas, matplotlib · Think out loud.

## Context
We trade US equity options on five venues (EX1–EX5) with two liquidity-taking strategies, **S1** and **S2**, which send **IOC** orders to take mispriced resting quotes. Every attempt is a race: other firms see the same quote, the first to reach the matching engine gets the fill, and sometimes the quote is pulled before anyone arrives. The latency team wants to know where we win, how much our speed matters, and whether S2 really is the better strategy. "Hit rate" is ambiguous: here it means **the fraction of our IOC take attempts that fill**, but it could also mean a market maker's quote hit rate or our share of OCC-cleared volume, so clarify it in the interview.

## Data
`python3 make_data.py` writes `../data/p05/take_attempts.parquet` (one row per IOC attempt, 20 trading days from 2026-10-19). Save plots to `../plots/`.

| column | meaning |
|---|---|
| `attempt_id` | attempt id |
| `date`, `ts_ns` | trading date; time the opportunity was detected (UTC epoch ns) |
| `venue`, `strategy`, `underlying` | `EX1`…`EX5`; `S1` / `S2`; underlying ticker |
| `edge_ticks`, `spread_ticks` | theoretical edge of the opportunity; bid-ask spread at detection (ticks) |
| `target_qty`, `filled_qty` | contracts we tried to take; contracts we got |
| `outcome` | `FILLED` / `PARTIAL` / `MISSED` |
| `reaction_ns` (Int64) | our reaction time: signal at our NIC → our order leaves our NIC (hardware timestamps) |
| `resp_ns` | our order leaves our NIC → exchange response |

A tick is $0.01 and the multiplier is 100, so 1 tick of edge on 1 contract = $1.

## Steps
**Step 1: Load, clean, define (≈25 min).** Audit the raw data and decide what to do with each problem you find. Define the hit rate (count-based; give the quantity-based version in one line). Build a table by venue and by strategy with 95% CIs.
*Deliverable:* data-quality counts with a decision for each, and the hit-rate table.

**Step 2: Is it stable over time? (≈25 min).** Plot the daily hit rate per venue. Flag day-over-day changes that are too large to be noise, and say whether each one was caused by us or by the market.
*Deliverable:* plot (a), a list of flagged (day, venue) breaks, and what they mean for the rest of the analysis.

**Step 3: The race curve (≈25 min).** For each venue, how does the hit rate depend on our reaction time? Bin the attempts, choose the time window deliberately, and show the uncertainty.
*Deliverable:* plot (b) and two sentences on how the venues differ.

**Step 4: S1 vs S2 (≈20 min).** A trader says: "S2 is our better strategy; its hit rate is higher." Check the claim, then produce one number that compares the two strategies fairly.
*Deliverable:* a small table, the fair number, a one-sentence explanation, and optionally plot (c).

## Wrap-up: 2 minutes, your conclusion + limitations
Say three conclusions and three limitations out loud, in English.

## Follow-ups (asked after you present)
1. What would being 200 ns faster be worth, in fills and $ per day? Which assumptions does your estimate need?
2. We shipped a software release on day 11. Did it help, and by how much? What else changed at the same time?
3. Your race curve pools strategies and edge sizes. How could that confound the latency effect, and how would you control for it?
4. Your CIs treat attempts as independent. Are they? What does that do to your break test?
5. How would you measure the value of latency with an experiment instead of observational data?
