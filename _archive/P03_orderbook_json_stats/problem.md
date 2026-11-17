# P03 — Rebuild an Order Book from a JSON Feed, then Describe the Market

**Time box: ~60 min** (Parts A–D are the core; E–F are stretch)

## Context

An exchange publishes a **price-level (L2) market-data feed** for one stock, `XYZ`, as JSON lines.
Our research team captured 30 minutes of it starting at the US open. Before anyone can study
microstructure on this data, we need a **correct book**: the best bid/ask and depth at every moment.

The capture was taken from a real network path, so it is not clean: messages can be repeated,
arrive slightly out of order, or never arrive. Traders will use your output to answer questions like
"how wide is the spread really?" and "does queue imbalance tell us where the price goes next?" — so a
silently wrong book is worse than a book that says "I don't know right now".

## Data

Run `python3 make_data.py` first. It writes to `data/p03/`.

### `feed.jsonl` — one JSON object per line

| field | type | meaning |
|---|---|---|
| `type` | str | `snapshot`, `update`, `trade`, or `heartbeat` |
| `seq` | int | exchange sequence number, +1 per message (heartbeats have **no** seq) |
| `ts` | int | exchange timestamp, nanoseconds since Unix epoch (UTC) |
| `side` | str | `update` only: `"B"` (bid) or `"A"` (ask) |
| `px` | float | `update`/`trade`: price in dollars. Tick size = $0.01 |
| `qty` | int | `update`: the **new total size** resting at that price level (0 = level removed). `trade`: shares traded |
| `bids`, `asks` | list of `[px, qty]` | `snapshot` only: the full book, best level first |

Semantics (as documented by the exchange):

- A **snapshot with seq S** is the full book **after all messages with seq < S**. Snapshots are published periodically.
- A **trade** message reports an execution; it does **not** change the book by itself. The level change caused by the trade is published as the next `update`.
- Sequence numbers are the authoritative order of events. Timestamps can be identical for many messages.

### Self-check files (don't use them inside your pipeline)

| file | columns | meaning |
|---|---|---|
| `truth_bbo.parquet` | seq, ts, bid, bid_qty, ask, ask_qty | the true best bid/ask after each seq |
| `truth_trades.parquet` | seq, aggressor | true aggressor side (`B` = buyer-initiated, `S` = seller-initiated) |

## Tasks

### Part A — Load and audit the feed (≈10 min)
1. Load the file robustly. Don't let one bad line kill the job; report what you skipped.
2. Inventory the message types and their counts.
3. Audit sequence integrity **in arrival order**: duplicates, messages that arrive out of order, and seq numbers that never arrive.
4. Check the price field: is it safe to use as a dictionary key?

**Deliverable:** a short printed audit report.

### Part B — Rebuild the book (≈20 min)
1. Process messages and maintain the bid and ask sides of the book.
2. Decide what to do about duplicates, out-of-order messages, and missing messages. State your policy before you code it.
3. Produce a BBO table: `seq, ts, bid, bid_qty, ask, ask_qty, stale` — one row per sequence number you processed. `stale=True` means you can't vouch for the book at that point.
4. Compare with `truth_bbo.parquet`: what % of your non-stale rows agree exactly? What happens if you skip your integrity logic and just apply messages in arrival order?

**Deliverable:** the BBO DataFrame + agreement numbers.

### Part C — Market statistics (≈10 min)
From your book and the trades, compute:
1. The spread in ticks: mean and distribution. Compute it two ways — averaged over **events** and averaged over **time** — and explain the difference.
2. Per-minute: number of messages, number of trades, traded volume, and mid-price realized volatility.
3. VWAP of all trades.
4. Classify each trade as buyer- or seller-initiated (no aggressor flag is given). Compare a quote-based rule with a tick rule; check against `truth_trades.parquet`.
5. Order-book imbalance at the top level and over the top 5 levels: `(bid_qty − ask_qty) / (bid_qty + ask_qty)`.

### Part D — Visualize (≈10 min)
1. For the busiest minute: best bid and best ask as step lines, trades overlaid and colored by inferred side.
2. Spread distribution (event-weighted vs time-weighted).
3. A depth heatmap: price level × time, colored by resting size, for a few minutes.
4. Imbalance (binned) vs the subsequent mid-price change.

### Part E — Stretch: is imbalance predictive?
Does top-of-book imbalance predict the direction of the **next** mid-price move?
Bin imbalance into deciles **using only the first half** of the session, then check whether the
relationship holds on the second half. Report something a trader can understand.

### Part F — Stretch: performance
How many messages per second does your rebuild process? Where is the hot path? How would you serve
"top 20 levels" queries quickly, and what changes if the book had 5,000 price levels per side?

## Follow-up questions the interviewer may ask
1. Why convert prices to integer ticks? Why not `int(px * 100)`?
2. Why not just sort the messages by timestamp and apply them?
3. A message with seq 1003 arrives but 1002 hasn't. What do you do, and for how long do you wait?
4. If an update says "qty is now 500", is applying a duplicate harmful?
5. When is an event-weighted statistic the right one, and when is a time-weighted one?
6. What information does an L2 feed not give you that an L3 (order-by-order) feed would? Why does a market maker care?
7. What if the snapshot itself arrives late, or a snapshot and incremental stream are on separate channels?
8. Python dicts aren't sorted — what does best-bid lookup cost in your design, and how would you make it O(1)?
9. How would you test this book builder so that a future refactor can't silently break it?
10. Your rebuild must run on 5,000 symbols at peak rates. What would you change?

## 考点（中文）
- **逐笔/L2 订单簿维护**：用 seq（不是 timestamp）排序；absolute-size update 的语义；整数 tick。
- **数据质量**：去重、乱序缓冲（reorder buffer）、gap → stale → snapshot 恢复；宁可标 stale 也不发错误的 book。
- **统计的正确口径**：event-weighted vs time-weighted；trade sign（quote rule / tick rule）；imbalance。
- **可视化**：step plot、heatmap、分箱图——能不能用图讲清楚市场。
- **研究方法**：in-sample/out-of-sample 切分，避免 look-ahead。
- **性能意识**：热路径、数据结构 tradeoff（dict + cached best vs heap vs 有序结构 vs tick 数组）。
