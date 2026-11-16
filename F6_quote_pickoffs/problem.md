# F6 — Quote Pick-offs: Markouts, Stale Fills, and the Value of Faster Requotes

**Time box: ~2 hours** · Python, pandas, numpy, matplotlib · the interviewer will help with syntax

## Context
We make markets in US equity options. That means we continuously quote a bid and an ask on many option series on three exchanges (EX1–EX3).

When the underlying stock moves, every option's fair value moves too, by roughly delta × move, and our quoting engine has to replace its quotes. Until the new quote is live at the exchange, the old one is **stale**. A fast liquidity taker can trade against it, and then we have been **picked off**.

The desk believes pick-offs are eating a large part of the spread we earn. They want to know how much, why, and what to fix first: a faster network path, or something in our software.

## Data
Run `python3 make_data.py`. It writes the files below to `../data/f6/` (one trading hour, 3 underlyings, 40 series each, 3 venues). All timestamps are int64 epoch nanoseconds (exchange time) on one PTP-synchronised clock.

| file | column | meaning |
|---|---|---|
| `underlying_ticks.parquet` | `ts_ns`, `underlying`, `mid` | each change of the underlying's mid price |
| `our_quotes.parquet` | `ts_ns` | time our (new) quote became live at the exchange |
| | `series_id`, `venue` | the quote stream; each row **replaces** the previous quote on that (series, venue) |
| | `bid`, `ask`, `size` | our prices ($) and displayed size (contracts) |
| `our_fills.parquet` | `fill_id`, `ts_ns` | execution id and time |
| | `series_id`, `venue`, `side` | `B` = we bought (our bid was hit), `S` = we sold (our ask was lifted) |
| | `price`, `qty` | execution price ($), contracts |
| `series.csv` | `series_id`, `underlying`, `cp`, `strike`, `delta` | static reference data |

The contract multiplier is 100. There is also a `_truth.parquet` for self-checking; don't open it until you're done.

## Steps

**Step 1: Load and audit (≈20 min).**
Check data quality, and anything a time-based join will need.
*Deliverable:* a data-quality summary, and the decisions you made.

**Step 2: Markouts (≈25 min).**
For each fill, compute its **markout** at horizons from 1 µs to 1 s: how much the fill was worth to us, valued at our fair value h later. Decide what "fair value" to mark against, and justify it.
*Deliverable:* the average markout in $ per contract by horizon, the total P&L for the hour, and one plot.

**Step 3: Which fills were stale? (≈25 min).**
Using **only timestamps**, classify each fill as fresh or stale. How many fills, contracts and dollars are stale? On which venues? How fast were the takers?
*Deliverable:* a table comparing fresh and stale fills.

**Step 4: Requote latency (≈30 min).**
For each requote, measure the time from the underlying move to our new quote being live. What does this latency depend on? Then estimate **P(a stale, mispriced quote gets picked off | our requote latency)**.
*Deliverable:* a latency table, one plot (two panels), and the curve.

**Step 5: What should we fix? (≈10 min).**
Estimate the $/hour saved by:
- (a) a network project that makes the whole path 2 µs faster;
- (b) a software change suggested by what you found in Step 4.

Recommend one of them to the desk in a few sentences.

## Discussion / follow-ups
1. Why mark against our own quote mid, rather than the next trade price or the exchange NBBO? What are the downsides?
2. What do the rising part and the plateau of your Step 4 curve each tell you about the takers?
3. Your stale rule uses only timestamps. When would it misclassify a fill?
4. Fresh fills also lose a little at long horizons. Is that the same problem? What would you do about it?
5. How would you prove your Step 5 change works before rolling it out to every venue?
6. Should we cancel quotes when the underlying moves instead of replacing them? Think about exchange throttles, mass quotes and market-maker protection.
7. How does this analysis relate to the taker side of the same race (F4)?
