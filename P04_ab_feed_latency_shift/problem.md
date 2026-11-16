# P04 — A/B Feed Arbitration and a Latency "Improvement" Claim

**Time box: ~60 min** · Python, pandas, numpy, matplotlib, scipy allowed · Think out loud.

## Context

Exchanges publish market data over **UDP multicast**. Because UDP has no retransmission,
every exchange sends the identical packet stream twice, over two physically separate paths
called **line A** and **line B**. Our feed handler listens to both and uses whichever copy of
each sequence number arrives first ("A/B arbitration"). Every microsecond matters: the faster
copy of a packet determines how early our strategies see the market.

We tapped both lines with capture NICs that stamp every packet in hardware. You have two
days of captures, covering 14:30–16:30 UTC each day.

> **Change log, network team:** *"We upgraded the line-B switch overnight between
> 2026-11-10 and 2026-11-11. Line B is now faster than line A."*

Your job is to describe the two feeds, then decide whether the network team's claim holds up.

## Data

Run `python3 make_data.py` first. It writes:

| file | contents |
|---|---|
| `../data/p04/feed_A.parquet` | every packet captured on line A, both days |
| `../data/p04/feed_B.parquet` | every packet captured on line B, both days |

Both files have the same schema, one row per received packet, in capture (arrival) order:

| column | type | meaning |
|---|---|---|
| `date` | category | trading date `YYYY-MM-DD` |
| `seq` | int64 | exchange sequence number; starts at 1 each day; the same seq on A and B is the same message |
| `exch_ns` | int64 | exchange send timestamp, ns since Unix epoch (exchange clock) |
| `rx_ns` | int64 | our NIC hardware receive timestamp, ns since Unix epoch (our clock, PTP-disciplined) |
| `msg_type` | category | ADD / CANCEL / MODIFY / TRADE / IMBALANCE |
| `bytes` | int16 | payload length in bytes |

Assume each capture is complete: if a packet is missing from a file, our NIC never received it.

## Tasks

### Part A: Load and check the data (10 min)
Load both files. For each line and day, report the row count, duplicate sequence numbers,
out-of-order arrivals, and missing sequence numbers (loss). How many seqs are missing on
**both** lines, and what does that mean operationally? Does loss look random or clustered?
**Deliverable:** a small integrity table and a sentence or two on each anomaly.

### Part B: Latency per line (10 min)
Define latency as `rx_ns - exch_ns`. Report p50 / p90 / p99 / p99.9 per line per day. Plot the
distributions in a way that makes the tail visible. Then judge whether this "one-way latency"
is trustworthy as an absolute number, and check it against the data.
**Deliverable:** a percentile table, one distribution figure, and your argument about whether
the absolute numbers can be trusted.

### Part C: A/B arbitration (10 min)
Build the arbitrated feed: for each seq, the first copy to arrive wins. Per day, report how
often each line wins (and how often only one line delivered). Compare arbitrated latency
percentiles with each line on its own. Show the distribution of the A-vs-B arrival difference.
**Deliverable:** a win-rate table, an arbitrated-vs-single-line percentile table, and one plot.

### Part D: Did the upgrade help? (15 min)
Answer the network team's claim with numbers.
1. Start with the naive before/after comparison of line B.
2. Is the difference statistically meaningful? Use a test and/or a confidence interval for the
   change in median and p99, and justify the method you choose.
3. Find out whether the two days are actually comparable. If they are not, design a fairer
   comparison and run it.
**Deliverable:** a clear yes/no/it-depends verdict, backed by your fairest comparison.

### Part E: Microbursts (10 min)
For each message, compute the exchange-side message rate as "number of messages sent in the
previous 100 µs". Plot latency against this rate. Is there a load level where latency
behaviour changes? Estimate it, and relate it to a plausible physical capacity.
**Deliverable:** a latency-vs-rate figure and an estimate of the "knee".

### Part F: Communicate (5 min)
Write a 5-bullet summary and choose **one** chart for the network engineers (who are not
statisticians). Include a concrete recommendation.

## Follow-up questions the interviewer may ask
1. Why do exchanges send A/B feeds at all? Why not just use TCP?
2. Write the arbitration logic for a live feed handler. What do you do when seq N is missing
   on both lines? How long do you wait for the other line before declaring a gap?
3. How are our clock and the exchange clock synchronised? What changes between hardware and
   software timestamps?
4. Why report p99/p99.9 instead of the mean? When would the mean actually be the right metric?
5. Bootstrap vs parametric tests: what does the bootstrap assume? Is that assumption true for
   packets inside a microburst?
6. Apart from load, what else could differ between the two days and confound the comparison
   (time of day, message mix, packet sizes, exchange-side changes, ...)?
7. Explain the "knee" with queueing theory. Why does latency blow up before utilisation
   reaches 100%?
8. Can a single line ever show a *better* p99 than the arbitrated feed built from it? How?
9. With 200 days of captures at 50M packets/day, how would you store this data and compute
   these statistics?

## 考点（中文）
- 数据完整性检查：duplicate / out-of-order / gap / 两条线同时丢包 → 需要 retransmission 或 snapshot
- latency 的正确统计：percentile、log 坐标、尾部 (CCDF)；能不能相信绝对的 one-way latency（时钟问题）
- A/B arbitration 的逻辑和价值
- 前后对比的统计推断：KS test、bootstrap（以及 bootstrap 的独立同分布假设）、confounding、Simpson's paradox，怎么设计更公平的对比
- microburst、queueing、capacity knee（排队论直觉）
- 给非统计背景的工程团队讲清结论（一张图 + 5 条结论）
