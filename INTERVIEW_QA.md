# 面试常见追问：数据处理、简单算法、可视化、沟通

每条 2–3 句英文，直接背。括号里是练习题里对应的例子，讲的时候带上一个具体数字，可信度更高。

## A. 数据处理

1. **You get a new dataset. What do you do first?**
   I look before I compute: shape, dtypes, ranges, nulls, duplicates and a few raw rows, and I ask what one row means and whose clock stamped it. Every problem I find goes into an audit table with a count and a decision, so every later number traces back to a choice. (F1–F5 Step 1)

2. **How do you handle missing values?**
   First ask why they're missing, because that decides the fix. A missing ACK can mean the venue never answered, which is a finding, not noise. I count them by group, flag or drop them, and never fill latencies or prices with a mean. (F1: no response vs not sent)

3. **How do you handle duplicates?**
   Define what a duplicate is (an identical log line, or the same sequence number twice), count them, then drop by that key. A sudden rise in duplicates is itself a system problem worth reporting. (F2: 215 duplicate seqs)

4. **The data is out of order. What do you do?**
   Never pair or difference by row order. Join on a key like order id, or sort by the authoritative order (exchange seq, not wall clock), and measure how far out of order things arrive, because that tells you how big a live buffer must be. (F1: 1,325 out-of-order lines; F2: 857 late arrivals)

5. **Do you remove outliers?**
   In latency data the outliers are usually the point, so I keep them and report percentiles, then look at when and where they happen. I only drop physically impossible records, like negative latency, and treat those as bugs to explain. (F1: gw-3's 1% tail was a periodic stall)

6. **How do you store timestamps?**
   As integer nanoseconds in UTC, converting only for display: float64 can't hold epoch nanoseconds exactly. I check the resolution, since µs and ns can be mixed, and whose clock it is; a cross-host difference is only as good as the clock sync, PTP. (F1: gw-4 logs ns)

7. **Can you use float prices?**
   Not as keys or for equality. I convert to integer ticks with `round(px / tick)`, never `int()`, which truncates 100.12999 to the wrong tick. (F2: `int` gets 4,351 prices wrong)

8. **How do you join trades to quotes when the timestamps don't line up?**
   With an as-of join: each trade takes the latest quote at or before it, by symbol (`merge_asof`). I also check how old that quote was, and use the book *before* the trade to avoid look-ahead. (F2: the book row at the trade's seq is the book before the trade)

9. **The file doesn't fit in memory. What now?**
   Read only the columns I need with compact dtypes, then stream in chunks and aggregate as I go, keeping only the state I need, such as open orders or a histogram. If it's still slow, split by file or symbol, process in parallel and merge the partial results. (F1 Step 5)

10. **Why vectorise in pandas?**
    A Python loop pays interpreter overhead on every row, while vectorised operations run in C over whole columns, often 10–100× faster. I only loop where the logic is truly sequential, like replaying an order book, and I measure before optimising.

11. **How do you know your pipeline is right?**
    I cross-check counts between steps, assert invariants like `bid < ask` and latency ≥ 0, and compare against ground truth or a naive alternative. Then I spot-check a few raw records by hand. (F2: 100% vs truth; the naive replay is 1.86% wrong)

## B. 统计与分析思路

12. **Mean or median for latency?**
    Latency is right-skewed, so the median describes the typical order and p99 the tail that costs money. The mean mixes the two and is dragged up by a few slow orders, but it's still the right number for capacity, since total time = mean × count. (F1: gw-3 mean 37 µs, median 8 µs)

13. **Why p99 and not the max?**
    The max is one event and jumps around; p99 is stable if there are enough samples, at least a few thousand so that 1% is tens of points. I show n next to every percentile, and I don't rank groups that have only a handful of samples.

14. **How do you compare before and after a change?**
    First check that the two periods are comparable: same load, same mix, same time of day. If they aren't, compare like with like: pair the same packets or orders, or bucket by the confounder and compare within buckets. (F3: pair each packet on line A and line B)

15. **What is a confounder? Give an example.**
    It's a third variable that moves with the thing you changed and also drives the outcome. In F3, day two had about three times the bursts, so line B's p99 rose from 70 to 111 µs, but untouched line A rose too. On the same packets, B got faster at every load level.

16. **What is Simpson's paradox?**
    An overall comparison reverses inside every subgroup because the mix differs. S2's pooled hit rate beat S1's, 0.748 vs 0.679, but S1 won on every venue: 85% of S2's attempts were on the easiest venue. On S1's venue mix, S2 drops to 0.218. (F4)

17. **Is the difference significant?**
    I put an interval on it: a Wilson interval for a rate, or a bootstrap for a percentile or a difference. If the effect is smaller than the normal day-to-day swing, I call it inconclusive and say how much more data would settle it.

18. **Correlation or causation?**
    A correlation tells me where to look. To claim a cause I want a mechanism and a control that changes one factor and holds the rest fixed. In F5, session s4 had the same build, kernel and rack but Nagle off, and showed no 40 ms orders at all.

19. **The series jumps on one day. What do you do?**
    That's a structural break: I don't average across it. I find it on a daily plot, tie it to an event (our release, or a new competitor), and analyse each regime separately. (F4: day 11 was our release, day 16 was external)

20. **How do you avoid look-ahead or overfitting?**
    I fit any thresholds or buckets on the first part of the data and test on the rest, using only information available at decision time. (F2: imbalance deciles fit on the first half, 66% correct on the second)

21. **How do you find an intermittent problem?**
    I plot the slow events against time; if they look regular, I take the gaps between them to get the period and fold time by it. If every slow event lands in the same phase, it's a scheduled job, not load. (F1: gw-3, first 120 ms of every 10 s)

## C. 简单算法与数据结构

22. **What data structure do you use for an order book?**
    For a price-level book, each side is a map from price in ticks to size, plus the best price. `dict` + `max` is fine for ~50 levels; with many levels or tight latency, use a cached best, a sorted structure, or an array indexed by tick. For order by order, use a hash map from order id to a node in a FIFO list per price level, so add, cancel and reduce are O(1).

23. **Messages arrive out of order in a live feed. What do you do?**
    I hold early messages in a min-heap keyed by seq, release them while the top equals the next expected seq, and give up after a time budget, marking the book stale until a snapshot. The budget trades added delay against staleness, so I pick it from data. (F2 Step 5: 0 ms → stale 1,592 of 1,800 s; 50 ms = offline result)

24. **How do you compute a percentile on a stream, or across machines?**
    Keep a fixed log-bucket histogram (HDR) or a t-digest: bounded memory, error bounded by the bucket width, and it merges by adding counts. Never average percentiles from different machines. (F1: averaging four p99s gives 343 µs; the true value is 18 µs)

25. **How do you count events in a sliding window, for example for a rate limit?**
    Keep a deque of timestamps: append each new one, and pop from the left while the oldest is outside the window. The length is the count, at O(1) amortised per event.

26. **How do you match request and response events?**
    I keep a hash map from id to the first event; when its partner arrives, I compute the difference and delete the entry. Memory equals the number in flight, and I evict entries after a TTL so orphans don't leak. (F1 Step 5)

27. **How do you group events into incidents?**
    I sort by time and start a new incident whenever the gap to the previous event exceeds a threshold, which is a cumulative sum of `gap > T`. It's one pass, and I pick the threshold from the plot. (F1: 262 unanswered ARCX orders in 20 s)

28. **Threads or processes?**
    In Python, CPU-bound work like parsing needs processes because of the GIL; I/O-bound work can use threads or asyncio. I measure it, because on small inputs process start-up cancels the gain. (F1: four processes were no faster on 24 MB)

29. **How do you scale a low-latency handler across cores?**
    I shard by symbol and give each book a single writer thread, fed by a lock-free single-producer ring buffer, so the hot path takes no locks. Readers get snapshots through a seqlock or as messages, and memory comes from preallocated pools.

## D. 可视化

30. **How do you plot a latency distribution?**
    A histogram with log-spaced bins shows the shape, such as two modes; an ECDF, or 1 − ECDF on log axes, compares tails across groups. I avoid box plots for latency because they hide both bimodality and the tail. (F1, F3, F5)

31. **How do you compare many groups?**
    A dot plot with confidence intervals, sorted by value, or small multiples with shared axes. Not overlaid lines in many colours, and never a pie. (F4: S1 vs S2 by venue)

32. **How do you plot a time series with an incident?**
    A line over time at a sensible resolution (per second or per minute), with the incident window shaded and the takeaway in the title. (F2: stale period shaded on the message-rate plot)

33. **Two metrics with different units: how do you plot them?**
    Two panels sharing the x-axis, not a dual y-axis, because two scales let the chart suggest a relationship that isn't in the data.

34. **How do you show a relationship between two variables?**
    A scatter plot, using hexbin or transparency when there are many points, plus a binned median line so the trend is readable. I use a log scale if the values span orders of magnitude. (F3: latency vs load, with the knee)

35. **When do you use a log scale?**
    When values span orders of magnitude, or what matters is ratios and tails, as with latency or sizes. I label the ticks with plain numbers (1, 10, 100 µs) and put "log" in the axis label.

36. **How do you plot quotes over time?**
    As a step plot, because a quote holds until it changes; a straight line would invent prices in between. I zoom into a few seconds so individual updates and trades are visible. (F2: busiest 3 s)

37. **How do you plot a discrete variable like the spread in ticks?**
    A bar per value rather than a histogram with arbitrary bins. If the weighting matters (per event vs per unit time), I show both side by side. (F2: 1 tick for 60% of updates but 77% of the time)

38. **When do you use a heatmap?**
    For a metric over two binned dimensions, such as hour × venue. I use a sequential colour map for magnitudes, and a diverging one centred on zero for differences.

39. **What makes a good chart in an interview?**
    A title that states the takeaway, axes with units, only the series I need, and colour with meaning: one highlight, the rest grey. I say why I chose the chart before I show it.

## E. 沟通

40. **How do you present a finding?**
    Answer first, then the evidence, then the caveats: one sentence with the number, the chart that shows it, and what would change my mind. I use the listener's units: µs for engineers, hit rate or dollars for traders.

41. **Which limitations do you mention?**
    Three kinds: scope (one day, one venue, synthetic data), measurement (whose clock, what resolution), and inference (correlation, a mechanism not yet tested). Then I say what I'd do next to remove the biggest one.

42. **The result contradicts what you expected. What do you do?**
    Check the data first (units, clocks, filters), then look for a confounder before concluding anything. In F3, the naive comparison said the upgrade made line B slower; pairing packets showed it was faster at every load.

43. **The interviewer pushes back on your choice. How do you respond?**
    I restate the trade-off I made and the alternative, and say when theirs would be better; if it's cheap, I try it and compare. Changing my mind for a reason is fine; defending a choice blindly isn't.

44. **You're running out of time. What do you do?**
    I say what's left and in what order, and deliver the most important result end to end first, leaving the polish. A correct, simple answer with stated limits beats an unfinished fancy one.
