# F1 — Order Processing Latency from Gateway Logs

**Time box: ~2 hours** · Python, pandas, matplotlib (the interviewer will help with syntax)

## Context
Our strategies send orders through four order gateways (`gw-1` … `gw-4`). A gateway receives an order (`IN`), sends it to an exchange (`OUT`), and then gets the exchange's answer (`ACK` = accepted, `REJ` = rejected). The trading desk asks: *"What is our average order processing time, and is anything wrong?"* You have 15 minutes of raw, multi-threaded gateway logs around the US open (14:30 UTC). Real logs are never perfectly clean.

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
| timestamp | UTC time the line was logged on the gateway host (fractional seconds) |
| gateway / level / thread | `gw-1..4` / `DEBUG INFO WARN ERROR` / `[t1..t4]` |
| event | `IN` from strategy · `OUT` to exchange · `ACK` accepted · `REJ` rejected |
| msg type | `NewOrder` or `Cancel` (on `IN` / `OUT` lines only) |
| `clid` | client order id, unique per order |
| `venue` | `XNAS`, `ARCX`, `BATS` (on `OUT`) |
| `reason` / `exch_id` | reject reason (`REJ`) / exchange order id (`ACK`) |

The file also contains heartbeats, stats lines, errors with tracebacks, and other things a real log contains. Don't assume every line is well-formed.

## Steps
1. **Parse.** Stream the file, as if it didn't fit in memory, into a DataFrame with one row per order event. Deliverable: a data-quality summary (lines parsed / skipped / malformed / duplicated / out of time order) and how you handled each case.
2. **One row per order.** Pair each order's events. Compute `internal latency = OUT − IN` and `venue round trip = (ACK or REJ) − OUT`, and give every order a status. Deliverable: the per-order table and a status count. State how you treat missing, duplicated or impossible timestamps.
3. **Statistics.** Deliverable: a table of count / mean / p50 / p99 / p99.9 / max for internal latency by gateway × msg type, and for round trip by venue. Then answer the desk: what is "the" average processing time, and is the mean the right number to report?
4. **Diagnose.** If a gateway or venue behaves differently, find *when* and *why*. Deliverable: one figure that makes the root cause obvious to an engineer, plus one or two sentences per finding. Also look at orders that never received an answer, and at the rejects.
5. **Scale (discussion; a little code if time allows).** Production logs are ~200 GB/day across 40 gateways. How would you compute the same numbers daily, or in real time, and combine the 40 machines' results?

## Discussion / follow-ups
1. Each gateway reports its own p99. Can you average the 40 p99s? What do you ship to the central node instead?
2. IN/OUT/ACK are all timestamped on the gateway host. Which latencies can you trust? What changes when two timestamps come from different machines (NTP vs PTP)?
3. One gateway logs nanoseconds and the others log microseconds. What does 1 µs resolution do to a 5–8 µs measurement?
4. An order never got an ACK or REJ. Can the system just resend it? What should happen instead?
5. What is the memory complexity of your parser and pairing? What state does a fully streaming version need?
6. How fast is your parser (lines/s)? How would you find where the time goes, and make it faster or concurrent?
7. How would you alert on these problems in real time without paging people for every outlier?
