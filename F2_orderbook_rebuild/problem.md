# F2: Rebuild an Order Book from a JSON Feed

**Time box: ~2 hours (aim for ~100–120 lines of core code).** Python, pandas, matplotlib. Think out loud; the interviewer can help with syntax.

## Context
An exchange publishes a price-level (L2) market-data feed for one stock as JSON lines, and we captured the first 30 minutes after the US open. Research needs the correct best bid and best ask at every moment.
The capture came over a real network, so messages can be repeated, reordered or lost. A book that says "I can't vouch for this right now" is better than a silently wrong one.

## Data
Run `python3 make_data.py` first. It writes `../data/p03/`. **`feed.jsonl`** has one JSON object per line:

| field | meaning |
|---|---|
| `type` | `snapshot`, `update`, `trade` or `heartbeat` |
| `seq` | exchange sequence number, +1 per message (heartbeats have none) |
| `ts` | exchange time, ns since epoch (UTC) |
| `side` | `update` only: `B` (bid) or `A` (ask) |
| `px` | `update` / `trade`: price in $ (tick size $0.01) |
| `qty` | `update`: new size of the level. `trade`: shares traded |
| `bids`, `asks` | `snapshot` only: the full book as `[px, qty]` pairs, best first |

Exchange semantics:
- A snapshot with seq S is the book **after all messages with seq < S**.
- `qty` in an update is the **new absolute size** of that price level; `qty = 0` deletes the level.
- A trade does not change the book by itself; the level change it causes arrives as a separate update.

Self-check only (not for the pipeline): `truth_bbo.parquet` (`seq, ts, bid, bid_qty, ask, ask_qty`: the true BBO after each seq, prices in $) and `truth_trades.parquet` (`seq, aggressor` = `B` / `S`).

## Steps
**Step 1: Load and audit (~15 min).** Load the file without letting one bad line kill the job. Count message types, duplicate seqs, out-of-order arrivals and seqs that never arrive. Is `px` safe to use as a dict key?
*Deliverable:* a short printed audit.

**Step 2: Rebuild the book (~40 min).** State your policy for duplicates, reordering and gaps before you code it.
*Deliverable:* a BBO DataFrame `seq, ts, bid, bid_qty, ask, ask_qty, stale`; the % of non-stale rows that match `truth_bbo`; what goes wrong if you apply messages in arrival order; one chart showing whether the feed can be trusted over time.

**Step 3: Describe the market (~30 min).** Mean spread in ticks, per update and per unit of time; explain the difference. Classify each trade as buyer- or seller-initiated (no flag is given) and check against `truth_trades`.
*Deliverable:* the numbers, a chart of bid, ask and trades over the busiest few seconds, and a chart of the spread distribution.

**Step 4: Is the book informative? (~20 min).** Top-of-book imbalance is `(bid_qty − ask_qty) / (bid_qty + ask_qty)`. Does it predict the direction of the next mid-price move? Build deciles on the first half of the session and test them on the second.
*Deliverable:* a decile chart and a one-sentence answer for a trader.

**Step 5: Live mode (optional, ~20 min, or talk it through).** A live handler can't sort a file it hasn't received yet. Release messages in seq order, holding early ones while you wait a bounded time for a missing seq. How long should you wait? Measure the trade-off between how long the book is stale and how long messages are held, and check that the live book is never *wrong*. How much headroom does your handler have over the feed's busiest second?
*Deliverable:* a small table (wait budget → stale seconds, share of messages held, worst hold) and your choice of budget.

**Wrap-up: 2 minutes, your conclusion + limitations.**

## Follow-ups
1. Why integer ticks, and why not `int(px * 100)`?
2. Why not just sort the messages by timestamp?
3. A seq is truly lost. How do you recover faster than waiting for the next snapshot?
4. An update says "qty is now 500". Is a duplicate of it harmful?
5. Best price per side: dict + `max`, cached best, heap, sorted tree or tick-indexed array. What does each cost?
6. Now 5,000 symbols on a multi-core box. How do you split the work so the hot path takes no locks? With an order-by-order (L3) feed, how do add, cancel and modify map onto your data structures?
