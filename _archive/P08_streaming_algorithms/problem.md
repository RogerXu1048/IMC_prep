# P08 — Streaming Algorithms on Market-Data Captures

**Time box: ~60 min** (pick 3 of the 5 parts in a real interview; all 5 for practice)

## Context

We capture the same exchange's market data on **four network ports**. Each port writes its own file
in the order packets were *received*. Downstream research jobs and live monitors need:

- one stream in **event-time order**,
- alerts for **microbursts** (sudden spikes in message rate that overflow buffers and queues),
- a check that our own order sends respect the exchange's **throttle**,
- **live latency percentiles**, and
- the symbols that are suddenly **hot**.

These run continuously, so assume the data is a **stream**: you can't load everything and sort it,
and memory must stay bounded. This is an algorithm-design interview: for every part, first state
your assumptions, give a brute-force answer and its complexity, then improve it.
Plain Python (standard library) is expected; pandas is not needed.

## Data

Run `python3 make_data.py` first. It writes to `data/p08/`.

### `port_{0,1,2,3}.jsonl` — market data, one message per line, in **arrival order** for that port

| field | type | meaning |
|---|---|---|
| `ts_ns` | int | exchange event time, ns since Unix epoch |
| `port` | int | capture port 0–3 |
| `seq` | int | per-port packet sequence number, in arrival order |
| `sym` | str | symbol |
| `type` | str | `quote`, `trade`, or `status` |
| `lat_us` | int | measured feed latency for this message, integer microseconds |

The network team says delivery on a port can be out of event-time order by **at most 2 ms**
"almost always".

### `orders.jsonl` — our order sends, sorted by `ts_ns` (ties in file order)

| field | type | meaning |
|---|---|---|
| `ts_ns` | int | time the order was sent, ns since Unix epoch |
| `session` | str | order-entry session (`S1`, `S2`, `S3`) |
| `order_id` | str | unique id |

**Exchange throttle rule:** per session, at most **50 orders in any rolling 1-second window**.
Precisely: the order sent at time `t` violates the rule if it is the 51st-or-later order of its
session whose send time lies in `(t − 1 s, t]`.

## Tasks

### Part A — Merge the four ports into one event-time-ordered stream
1. Write it with generators so memory doesn't depend on file size.
2. First assume each port file is already sorted by `ts_ns`. How do you merge k sorted streams? Complexity?
3. Check that assumption on the real data. If it doesn't hold, design a fix that uses the "≤ 2 ms" guarantee. What memory does it need? What do you do with a message that violates the guarantee?

**Deliverable:** a merged generator, a count of messages that couldn't be placed in order, and the complexity of each approach.

### Part B — Microbursts
1. What is the maximum number of messages in any 1 ms window (over the merged stream)? Brute force first, then O(N).
2. List every burst where some 1 ms window contains more than 50 messages; merge overlapping windows into one burst and report start time, duration, and peak count.

### Part C — Throttle compliance
1. For each session, find all orders that violated the rule and the first violating order.
2. Design a `RateLimiter` class with `allow(t) -> bool` that the order-sending code calls before every send so that we **never** violate the rule. Give the complexity.
3. A colleague proposes a token bucket with capacity 50 and refill 50/s instead. Is it equivalent? Show it with the data.

### Part D — Rolling latency percentiles
Maintain p50 and p99 of `lat_us` over the **last 1,000 messages** of the merged stream, updated on every message
(use nearest-rank: the p-th percentile of n values is the ⌈p·n/100⌉-th smallest).
1. Brute force and its cost.
2. A faster design. (Hint-free: think about what you know about the values' range.)
3. A dedicated sliding-window **median** design that doesn't depend on the value range.
4. Verify the fast versions against brute force. Plot the rolling p50/p99 over time — what do you see?

### Part E — Hot symbols
1. Top-5 symbols by message count over the **last 60 seconds**, updatable per message.
2. Now memory is capped at ~20 counters regardless of how many symbols exist. Give an approximate algorithm with a guarantee. How would a Count-Min sketch differ?

## Follow-up questions the interviewer may ask
1. `heapq.merge` is O(N log k). Can you do better if k = 2? If k = 10,000?
2. How do you choose the 2 ms reorder bound in production? What's the trade-off of making it bigger?
3. Your watermark stalls if one port goes silent. How do you fix that? (Think idle sources / heartbeats.)
4. In Part B, does the definition `(t − 1 ms, t]` vs `[t, t + 1 ms)` change the answer? What about time-bucketed counts (per calendar millisecond) — what can they miss?
5. Why is a token bucket the industry default even though it isn't a rolling-window limiter? When do exchanges use which?
6. Your Fenwick tree has 32,768 buckets. What if latencies are floats in nanoseconds with a huge range? (HDR histogram, log-bucketing, t-digest.)
7. Can you merge two p99s computed on two shards into a global p99? What can you merge?
8. What's the error guarantee of Misra–Gries, and why can it only *under*-count? Count-Min only *over*-counts — why?
9. Which of these parts would you run in the hot path of a trading system, and which offline?
10. How would you unit test each of these against a brute-force oracle?

## 考点（中文）
- **k-way merge / heap / generator**：O(N log k)，惰性求值，内存有界。
- **乱序流处理**：reorder buffer + watermark + late side output（对应 Flink/Kafka Streams 的 event-time 概念）。
- **滑动窗口 / 双指针**：microburst、throttle；窗口边界（开区间/闭区间）的 off-by-one。
- **限流器设计**：sliding log vs token bucket 的语义差别。
- **流式分位数**：Fenwick tree 的 k-th 查询、双堆 + lazy deletion、可合并的直方图（HDR）vs 不可合并的 p99。
- **Heavy hitters**：精确滑窗计数 vs Misra–Gries / Count-Min 的近似保证。每一题都要说复杂度并用 brute force 做对拍测试。
