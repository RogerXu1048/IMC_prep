# F3: A/B Feed Latency. Did the Line-B Upgrade Help?

**Time box: ~2 hours (aim for ~100 lines of core code)** · Python, pandas, numpy, matplotlib · Think out loud.

## Context
The exchange sends every multicast packet twice, on two independent network paths (line A and line B), because UDP never retransmits; our feed handler uses whichever copy arrives first. We captured both lines on 2026-11-10 and 2026-11-11, 14:30–16:30 UTC, with hardware NIC timestamps. The network team upgraded the line-B switch overnight between the two days and claims B is now faster. Decide whether the data supports that claim, and what else the network team should know.

## Data
Run `python3 make_data.py` once. It writes `../data/p04/feed_A.parquet` and `../data/p04/feed_B.parquet`: one row per packet received on that line, in arrival order. A seq missing from a file was never received on that line.

| column | meaning |
|---|---|
| `date` | trading date `YYYY-MM-DD` (category) |
| `seq` | exchange sequence number, starts at 1 each day; same seq on A and B = same message |
| `exch_ns` | exchange send time, ns since epoch (exchange clock) |
| `rx_ns` | our NIC receive time, ns since epoch (our clock) |
| `msg_type`, `bytes` | ADD / CANCEL / MODIFY / TRADE / IMBALANCE, payload length |

## Steps
**Step 1: Load and audit (~25 min).** Per line and day, count packets, duplicates, out-of-order arrivals and missing seqs. How many seqs were lost on **both** lines, and what must the feed handler do about them? Is the loss random or clustered?
*Deliverable:* one audit table and one sentence per problem you found.

**Step 2: Latency per line and day (~25 min).** Define latency as `rx_ns − exch_ns` and report p50 / p99 for each line × day. Plot the distributions so the tail is visible. Can the absolute one-way numbers be trusted? Check against the data, and say which comparisons stay valid.
*Deliverable:* a percentile table, one figure, your argument.

**Step 3: Did the upgrade help? (~40 min).** Start with the naive before/after comparison of line B. Then decide whether the two days are comparable, and design and run the fairest comparison you can. As a load measure, compute for every message how many messages the exchange sent in the previous 100 µs (it must be fast on ~770k rows). Also report, per day, the share of packets where B arrives first.
*Deliverable:* a yes/no verdict backed by your fairest comparison, and one figure.

**Step 4: Load effect (~15 min).** How does latency depend on load? Estimate where queueing starts, and show it with one chart.
*Deliverable:* one figure and a number.

**Wrap-up: 2 minutes, your conclusion + limitations.** Say it as you would to the network engineers, who are not statisticians.

## Follow-ups
1. Why A/B multicast instead of TCP? What happens when a seq is lost on both lines?
2. How do our clock and the exchange clock stay in sync? What changes with software timestamps instead of hardware ones?
3. Why p99 and not the mean? Can you average p99s across days or machines?
4. Can one line on its own show a *better* p99 than the first-arrival (arbitrated) feed? How?
5. With 200 days of captures at 50M packets per day, how would you store this and compute these statistics?
