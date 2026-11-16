# P01 — Order Gateway Logs: How Fast Do We Process Orders?

**Time box: ~60 min** · Python, pandas, matplotlib (the interviewer will help with syntax)

## Context

Our strategies send orders to four **order gateways** (`gw-1` … `gw-4`). A gateway receives an
order from a strategy (`IN`), validates/encodes it and sends it to an exchange (`OUT`), then
receives the exchange's answer (`ACK` = accepted, `REJ` = rejected). Every microsecond inside
the gateway is a microsecond a competitor can use to beat us to a trade, so the trading desk
asks a simple question:

> "What is our average order processing time — and is anything wrong?"

You have 15 minutes of raw gateway logs around the US market open (14:30 UTC). The logs are
the real thing: multi-threaded, noisy and not perfectly clean.

## Data

Run `python3 make_data.py` first. It writes:

`data/p01/gateway.log` (~275k lines, ~25 MB). Typical lines:

```
2026-11-12 14:30:00.123456 gw-2 INFO  [t3] IN  NewOrder clid=G2-000123 sym=AAPL side=BUY px=231.45 qty=100
2026-11-12 14:30:00.123481 gw-2 INFO  [t3] OUT NewOrder clid=G2-000123 venue=XNAS
2026-11-12 14:30:00.123902 gw-2 INFO  [t1] ACK clid=G2-000123 exch_id=8812733
2026-11-12 14:30:00.124102 gw-2 WARN  [t1] REJ clid=G2-000777 reason=PRICE_BAND
2026-11-12 14:30:00.200000 gw-2 INFO  [t2] IN  Cancel clid=G2-000124 orig=G2-000100 sym=AAPL
```

| field | type | meaning |
|---|---|---|
| timestamp | `YYYY-MM-DD HH:MM:SS.fraction`, UTC | time the line was logged on that gateway host |
| gateway | `gw-1`..`gw-4` | which gateway process wrote the line |
| level | `DEBUG/INFO/WARN/ERROR` | log level |
| thread | `[t1]`..`[t4]` | logging thread |
| event | `IN`, `OUT`, `ACK`, `REJ` | received from strategy / sent to exchange / exchange accepted / exchange rejected |
| msg type | `NewOrder`, `Cancel` | only on `IN` / `OUT` lines |
| `clid` | str | client order id, unique per order (prefix = gateway) |
| `sym, side, px, qty` | | order details (`IN NewOrder` only) |
| `orig` | str | the order being cancelled (`IN Cancel` only) |
| `venue` | `XNAS`, `ARCX`, `BATS` | destination exchange (`OUT` only) |
| `exch_id` / `reason` | | exchange order id (`ACK`) / reject reason (`REJ`) |

The file also contains heartbeats, periodic stats lines, errors, and whatever else a real
production log contains. Do not assume every line is well-formed.

## Tasks

**Part A — Parse (≈10 min).** Write a parser that streams the file (do not assume it fits in
memory in the real system) and produces a tidy DataFrame with one row per order event.
Report a data-quality summary: how many lines were parsed, skipped, malformed, duplicated.

**Part B — One row per order (≈10 min).** Pair the events of each order and compute
* `internal latency` = `OUT − IN` (time spent inside our gateway),
* `exchange round trip` = response (`ACK`/`REJ`) − `OUT`,
* `total` = response − `IN`.
Give every order a status (e.g. acked, rejected, no response, …). State how you handle orders
with missing, duplicated or impossible (negative) timestamps.

**Part C — Statistics (≈10 min).** Produce a table with `count, mean, p50, p90, p99, p99.9, max`
for internal latency by gateway × message type, and for exchange round trip by venue.
Answer the desk's question: what is "the" average processing time? Is the mean the right number?

**Part D — Visualize and diagnose (≈15 min).** Plot the latency distributions so that both the
body and the tail are visible (think about axis scales). Compare gateways. If one gateway
behaves differently, investigate *when* and *why* its slow orders happen and produce one plot
that makes the root cause obvious to an engineer.

**Part E — Orders without an answer (≈5 min).** How many orders never received an exchange
response? Where and when did they happen? Is it random? What should a production system do
about an order in this state? Also look at rejects — anything noteworthy?

**Part F — Scale it (discussion + optional code).** In production the logs are ~200 GB/day
spread over 40 gateways. How would you compute the same latency numbers every day (and ideally
in real time)? How do you combine the results from 40 machines?

## Follow-up questions the interviewer may ask

1. The desk asked for the *average*. Why might p50 / p99 / p99.9 be more useful? When is the mean still useful?
2. Each gateway reports its own p99. Can you average the 40 p99s to get the global p99? What do you do instead?
3. IN and OUT are timestamped on the same host; ACK comes from the exchange. Which latencies can you trust, and what changes if two timestamps come from different machines (NTP vs PTP)?
4. Some gateways log microseconds, one logs nanoseconds. What does the timestamp resolution do to your 5–8 µs measurements?
5. What is *coordinated omission* and could it affect a latency benchmark like this?
6. The log is written by an asynchronous logger. Is the timestamp the time the event happened or the time it was logged? Why does it matter?
7. Regex vs `str.split` for parsing — which is faster and why? How fast is your parser (lines/s)? Where is the time going?
8. Your parser runs on a 200 GB file. What is the memory complexity of your approach? What state must you keep?
9. How would you alert on the problems you found, in real time, without paging people for every outlier?

## 考点（中文）

- **文本解析与数据清洗**：regex/split、generator 流式读取、malformed/重复/乱序/多行 traceback/不同时间精度的处理，以及数据质量报告。
- **Latency 统计学**：mean vs median vs tail percentile，分组切片（gateway × type、venue、时间段），为什么不能平均 percentile。
- **可视化与 root cause**：log scale、ECDF/CCDF、按时间“折叠”(fold by period) 找周期性问题；用一张图说服工程师。
- **生产系统思维**：无响应订单的状态管理（timeout、reconcile），时钟同步，异步 logger，规模化（streaming、sharding、HDRHistogram/t-digest 可合并 sketch）。
