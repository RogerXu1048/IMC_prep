# F3 — A/B Feed Latency: Did the Line-B Upgrade Help?

**Time box: ~2 hours** · Python, pandas, numpy, matplotlib · Think out loud; the interviewer will help with syntax.

## Context

Exchanges publish market data over UDP multicast. UDP never retransmits, so the exchange sends every packet twice, over two separate network paths: **line A** and **line B**. Our feed handler listens to both lines and uses whichever copy of each sequence number arrives first; this is called **A/B arbitration**. We captured both lines for two days with NICs that timestamp every packet in hardware.

The network team wrote in the change log: *"We upgraded the line-B switch overnight between 2026-11-10 and 2026-11-11. Line B is now faster than line A."* Decide whether the data supports that claim, and what else the network team should know.

## Data

Run `python3 make_data.py` first. It writes `../data/p04/feed_A.parquet` and `../data/p04/feed_B.parquet`. Each file has one row per received packet, in arrival order, covering 14:30–16:30 UTC on both days.

| column | type | meaning |
|---|---|---|
| `date` | category | trading date `YYYY-MM-DD` |
| `seq` | int64 | exchange sequence number, starting at 1 each day; the same seq on A and B is the same message |
| `exch_ns` | int64 | exchange send time, ns since epoch (exchange clock) |
| `rx_ns` | int64 | our NIC receive time, ns since epoch (our clock, PTP-disciplined) |
| `msg_type` | category | ADD / CANCEL / MODIFY / TRADE / IMBALANCE |
| `bytes` | int16 | payload length |

If a packet is missing from a file, our NIC never received it on that line.

## Steps

**Step 1: Load and check integrity (~20 min).**
For each line and day, report packets, duplicates, out-of-order arrivals and lost sequence numbers. How many seqs were lost on **both** lines, and what does the feed handler have to do about them? Is the loss random or clustered?
*Deliverable:* one integrity table and one sentence per anomaly.

**Step 2: Per-line latency (~20 min).**
Define latency as `rx_ns − exch_ns`. Report p50 / p90 / p99 / p99.9 for each line × day, and plot the distributions so that the tail is visible. Can these absolute one-way numbers be trusted? Check against the data.
*Deliverable:* a percentile table, one figure, and your argument.

**Step 3: A/B arbitration (~25 min).**
Build the arbitrated feed, where the first copy of each seq wins. Per day, report the share of packets each line wins and how often only one line delivered. Compare arbitrated percentiles with each line on its own. Then write the **online** version a feed handler would run: two packet streams arrive in time order, and you must publish strictly in seq order, drop the second copy, and declare a gap when both lines lost a seq. Check that it agrees with the offline result.
*Deliverable:* a win-rate table, a percentile comparison, and a working online arbiter.

**Step 4: Did the upgrade help? (~35 min).**
Start with the naive before/after comparison. Then work out whether the two days are actually comparable. As a load measure, compute for every message how many messages the exchange sent in the previous 100 µs (it must be fast on ~800k rows). Design the fairest comparison you can, and run it. Plot latency against load, and say where queueing starts.
*Deliverable:* a yes/no verdict backed by your fairest comparison, a latency-vs-load figure, and a "knee" estimate.

**Step 5: Communicate (~10 min).**
Write 3–5 sentences for the network engineers, who are not statisticians. Include a concrete recommendation.

## Discussion / follow-ups

1. Why do exchanges use A/B multicast instead of TCP? What happens when a seq is lost on both lines?
2. How long should the live arbiter wait for the other line before it declares a gap? What does it cost if the timeout is too short? Too long?
3. How do our clock and the exchange clock stay in sync? What changes with software timestamps instead of hardware ones?
4. Why p99 / p99.9 and not the mean? Can you average p99s across days or machines?
5. Is a bootstrap confidence interval valid for packets inside a microburst? What would you do instead?
6. Can a single line ever show a *better* p99 than the arbitrated feed? How?
7. With 200 days of captures at 50M packets per day, how would you store this data and compute these statistics?
