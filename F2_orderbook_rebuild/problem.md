# F2: Rebuild an Order Book from a JSON Feed

**Time box: ~2 hours** · Python, pandas, matplotlib · Interviewers help with syntax. Think out loud.
## Context
An exchange publishes a price-level (L2) market-data feed for one stock, `XYZ`, as JSON lines.
We captured the first 30 minutes after the US open. Before research can use the data, we need a
**correct book**: the best bid and best ask at every moment. The capture came over a real network,
so messages may be repeated, may arrive out of order, or may never arrive. A silently wrong book is
worse than a book that says "I can't vouch for this right now."

## Data
Run `python3 make_data.py` first. It writes to `../data/p03/`.

**`feed.jsonl`**: one JSON object per line.

| field | type | meaning |
|---|---|---|
| `type` | str | `snapshot`, `update`, `trade`, or `heartbeat` |
| `seq` | int | exchange sequence number, +1 per message (heartbeats have no seq) |
| `ts` | int | exchange timestamp, ns since epoch (UTC) |
| `side` | str | `update` only: `B` (bid) or `A` (ask) |
| `px` | float | `update`/`trade`: price in $. Tick size = $0.01 |
| `qty` | int | `update`: the **new total size** at that price (0 = level removed). `trade`: shares traded |
| `bids`, `asks` | list of `[px, qty]` | `snapshot` only: the full book, best level first |

Exchange semantics:
- A **snapshot with seq S** is the full book **after all messages with seq < S**. Snapshots are periodic.
- A **trade** does not change the book by itself. The level change it caused arrives as the next `update`.
- Sequence numbers define the order of events. Many messages can share a timestamp.

**Self-check only** (not for the pipeline): `truth_bbo.parquet` (`seq, ts, bid, bid_qty, ask, ask_qty`,
the true BBO after each seq) and `truth_trades.parquet` (`seq, aggressor` = `B`/`S`).

## Steps

**Step 1: Load and audit (≈15 min).**
- Load the file without letting one bad line kill the job.
- Report the counts of message types, duplicate seqs, out-of-order arrivals, and seqs that never arrive.
- Is `px` safe to use as a dictionary key? *Deliverable:* a short printed audit.

**Step 2: Rebuild the book (≈40 min).** Implement a book (bid and ask sides, fast best price) and a
message handler in front of it.
- State your policy for duplicates, out-of-order messages and gaps **before** you code it.
- Output a BBO table `seq, ts, bid, bid_qty, ask, ask_qty, stale`.
- What % of your non-stale rows match `truth_bbo`?
- What goes wrong if you apply messages in arrival order with no integrity logic?
  *Deliverable:* BBO DataFrame, match rate, and naive-vs-correct comparison.

**Step 3: Market statistics (≈25 min).**
- Mean spread in ticks, **per event** and **per unit of time**; explain the difference.
- Classify each trade as buyer- or seller-initiated (no flag is given), check accuracy; VWAP of all trades.
- Plot best bid, best ask and trades (coloured by side) for the busiest minute.

**Step 4: Is the book predictive? (≈25 min).** Top-of-book imbalance is
`(bid_qty − ask_qty) / (bid_qty + ask_qty)`. Does it predict the direction of the **next** mid-price move?
- Build deciles on the first half of the session.
- Test them on the second half.
- Plot imbalance decile vs P(next move up); give the trader a one-sentence answer.

**Step 5: Performance discussion (≈5 min, no new code).** Messages per second of your rebuild, where the
hot path is, and how you'd serve best price and top-20 levels for 5,000 symbols.

## Discussion / follow-ups
1. Why integer ticks, and why not `int(px * 100)`?
2. Why not just sort messages by timestamp and apply them?
3. Seq 1003 arrives but 1002 hasn't. What do you do, and for how long do you wait?
4. An update says "qty is now 500". Is a duplicate of it harmful?
5. Best price: a dict with a cached best, a heap, a sorted tree, or a tick-indexed array? What does each cost?
6. What does an L2 feed not tell you that an L3 (order-by-order) feed would? Why does a market maker care?
7. How would you test the book builder so a refactor can't silently break it?
