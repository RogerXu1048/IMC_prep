# F1 — Order Latency from Gateway Logs

**Time box: ~2 hours (aim for ~100 lines of core code).** Python, pandas, matplotlib; the interviewer will help with syntax.

## Context
Our strategies send orders through four order gateways (`gw-1` … `gw-4`): a gateway receives an order (`IN`), sends it to an exchange (`OUT`), then gets the exchange's answer (`ACK` = accepted, `REJ` = rejected). The trading desk asks: *"What is our average order processing time, and is anything wrong?"* You have 15 minutes of raw, multi-threaded gateway logs around the US cash open (14:30 UTC). Real logs are never perfectly clean.

## Data
Run `python3 make_data.py` first. It writes `../data/p01/gateway.log` (~274k lines). Typical lines:
```
2026-11-12 14:30:00.123456 gw-2 INFO  [t3] IN  NewOrder clid=G2-000123 sym=AAPL side=BUY px=231.45 qty=100
2026-11-12 14:30:00.123481 gw-2 INFO  [t3] OUT NewOrder clid=G2-000123 venue=XNAS
2026-11-12 14:30:00.123902 gw-2 INFO  [t1] ACK clid=G2-000123 exch_id=8812733
2026-11-12 14:30:00.124102 gw-2 WARN  [t1] REJ clid=G2-000777 reason=PRICE_BAND
2026-11-12 14:30:00.200000 gw-2 INFO  [t2] IN  Cancel clid=G2-000124 orig=G2-000100 sym=AAPL
```
| field | meaning |
|---|---|
| timestamp | UTC time the line was written on the gateway host (fractional seconds) |
| gateway / level / thread | `gw-1..4` / `DEBUG INFO WARN ERROR` / `[t1..t4]` |
| event | `IN` from strategy · `OUT` to exchange · `ACK` accepted · `REJ` rejected |
| msg type | `NewOrder` or `Cancel` (on `IN` / `OUT` lines only) |
| `clid` | client order id, unique per order |
| `venue` / `reason` / `exch_id` | `XNAS ARCX BATS` (on `OUT`) / reject reason (on `REJ`) / exchange order id (on `ACK`) |

The file also contains heartbeats, stats lines and errors with tracebacks. Don't assume every line is well-formed.

## Steps
1. **Load and audit.** Parse the file into one row per order event. Deliverable: a data-quality summary: each kind of problem you found, how many lines it affects, and what you did about it.
2. **One row per order.** Compute `internal latency = OUT − IN` and `venue round trip = (ACK or REJ) − OUT`, and give every order a status. Deliverable: the per-order table and a count by status.
3. **Statistics.** Deliverable: p50 / p99 / mean of internal latency by gateway, and of round trip by venue. Make sure every comparison is fair. Then answer the desk: what is "the" processing time, and is the mean the right number to report?
4. **Diagnose.** If a gateway or venue behaves differently, find *when* and *why*. Also look at orders that never got an answer, and at the rejects. Deliverable: 2–3 figures whose titles state the takeaway, plus one or two sentences per finding.
5. **Scale (optional, ~15 min, or talk it through).** Production logs are ~200 GB/day from 40 gateways. Rework the parser so its memory doesn't grow with the file, process the gateways in parallel, and combine their latency distributions into one fleet-wide p99. Deliverable: the fleet p99, checked against Step 3; what you get if you average the per-gateway p99s instead; and whether the parallel version was actually faster.

**Wrap-up: 2 minutes, your conclusion + limitations.**

## Follow-up questions
1. IN, OUT and ACK are all timestamped on the gateway host. Which latencies can you trust? What changes if two timestamps come from different machines?
2. What does the timestamp resolution do to a measurement of a few microseconds?
3. An order never got an ACK or REJ. Can the system just resend it? What should happen instead?
4. Threads or processes for the parser, and why? When would the parallel version *not* be faster?
5. How would you alert on these problems in real time without paging on every outlier?
