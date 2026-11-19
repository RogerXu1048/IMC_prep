# F2 学习指南：从 JSON feed 重建 Order Book

> 先读一遍，再照 `problem.md` 从空白文件写 `solution.py`。下面的数字都来自 `python3 solution.py` 的实际输出和图标题（固定 seed，每次一样）。

## 0. 一句话题目
交易所的 L2 行情（JSON lines）有坏行、重复、乱序和丢包。先按 seq 把 order book 正确重建出来，诚实地标出 stale 区间；再用它描述市场：spread、成交方向，以及 imbalance 能不能预测下一跳。Step 2 是核心：先让 `replay()` 跑通、和 truth 对上，再做后面的统计和图。

| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify | 5 min | 确认 seq、snapshot、`qty` 的语义 |
| Step 1 Load + audit | 15 min | 审计打印：坏行、类型、重复、乱序、缺号、价格精度 |
| Step 2 Rebuild | 40 min | `replay()`、带 `stale` 的 BBO 表、和 truth 对比、naive 对比、图 (a) |
| Step 3 Market | 30 min | event- vs time-weighted spread、quote rule、图 (b)(c) |
| Step 4 Imbalance | 20 min | 前半段定 decile、后半段验证、图 (d) |
| 收尾 | 10 min | CONCLUSION + LIMITATIONS，留时间给追问 |
| Step 5 Live mode（加分） | 有时间写 20 min，否则口头讲 | reorder buffer 的 trade-off 表、吞吐、并发设计 |

## 1. Clarifying questions（英文，开场就问）
- "Is `qty` in an update the new total at that price, or a delta?"
- "Does a snapshot with seq S show the book before or after message S?"
- "Is this an offline analysis of the full file, or should I design it like a live feed handler?"

## 2. Step 1：Load + audit（15 min）
- **目标**：在定任何 policy 之前，先知道数据有哪些问题。
- **先说什么**："Before I design anything, I'll audit the feed in arrival order: bad lines, duplicates, late seqs, gaps, and how prices are encoded."

**决策与 trade-off**
- 逐行 `json.loads` 加 try/except，不用 `pd.read_json(lines=True)`：一行坏数据会让后者整个失败。代价是慢一点，二十多万行无所谓。
- 审计必须在 **arrival order** 上做，先排序的话乱序的证据就没了。"late" 的定义：之前已经到过更大的 seq。
- 价格转整数 tick 用 `round(px*100)`：float 有表示噪声，`int()` 是截断，`100.12999999999` 会变成 10012，差一个 tick。

**关键代码**
```python
tick = lambda px: round(px * 100)              # DECISION: round, not int(): int(100.12999999999 * 100) = 10012
for line in open(DATA / "feed.jsonl"):
    try:
        msgs.append(json.loads(line))
    except json.JSONDecodeError:           # DECISION: count and skip a bad line, never crash
        bad += 1
seq = pd.Series([m["seq"] for m in msgs if "seq" in m])        # heartbeats carry no seq
u = seq.drop_duplicates()                                       # CHECK: audit in ARRIVAL order, before sorting
# arrived late = (u.cummax() > u).sum()      missing seqs = set(range(1, seq.max() + 1)) - set(seq)
```

**自检**：`malformed lines=1`；types 为 update 187037、trade 28187、heartbeat 1799、snapshot 44；`duplicate seqs=215  arrived late=857  missing seqs=[101534, 101535, 101536]`；`noisy px=5408  int(px*100) wrong=4351`。

## 3. Step 2：重建 book（40 min，核心）
- **目标**：按 seq 顺序 replay，输出 BBO 表 `seq, ts, bid, bid_qty, ask, ask_qty, stale`，并证明它是对的。
- **先说什么**："Since I have the whole file offline, I'll dedupe and sort by seq, apply each message to two dicts of tick to size, and mark the book stale from any seq gap until the next snapshot."

**决策与 trade-off**
- **Sort by seq，先不写 reorder buffer**：离线时整个文件都在手上，排序最简单、最不容易错。实盘不能"看到未来"，需要一个有上限的 reorder buffer 加 timeout，放在 Step 5 做。不按 ts 排：很多消息 ts 相同，seq 才是交易所定义的顺序。
- **Book = 每边一个 dict（tick → qty），best 用 `max`/`min`**：每条消息 O(levels)，这里只有约 50 档，代码最短。生产里换成 cached best、sorted tree 或 tick-indexed array。
- **Gap → stale，直到下一个 snapshot**：丢掉的消息可能删掉了 best level，猜就会出错。stale 比错误好。

**关键代码**：`rebuild()` 先 `sorted({m["seq"]: m for m in msgs if "seq" in m}.values(), key=lambda m: m["seq"])`（dict 去重 + 按 seq 排序），再交给 `replay()`：
```python
for m in msgs:
    if m["type"] == "snapshot":            # snapshot seq S = book after all seq < S -> full reset
        book = {"B": {tick(p): q for p, q in m["bids"]}, "A": {tick(p): q for p, q in m["asks"]}}
    elif m["type"] == "update":            # qty = new absolute size, 0 = delete (trades don't touch the book)
        if m["qty"] > 0:
            book[m["side"]][tick(m["px"])] = m["qty"]
        else:
            book[m["side"]].pop(tick(m["px"]), None)
    stale = m["type"] != "snapshot" and (stale or m["seq"] != prev + 1)   # seq gap -> stale until snapshot
    prev, b, a = m["seq"], max(book["B"]), min(book["A"])   # DECISION: max/min is O(levels); ~50 levels, fine
```
验证：`truth_bbo` 的价格是美元，先 ×100 转成 tick，再按 seq 比较 4 列；另加 `assert (live.bid < live.ask).all()`。naive 对照只多一行：把同一个 `replay()` 用在**到达顺序**的原始消息上（不去重、不排序）。

**自检**
- `stale rows=3,464`（seq 101537–105000，seq 105001 的 snapshot 到了才恢复）；`live rows matching truth=100.00%`
- `naive arrival-order replay: 1.86% rows wrong, 2,461 crossed`。大部分错误不在 gap 附近，而是乱序和重复造成的：晚到的旧 update 把已经删掉的 level "复活"（phantom level），于是 bid ≥ ask。

## 4. Step 3：描述市场（30 min）
- **目标**：spread 分别按事件和按时间平均；用 quote rule 判断成交方向并检查准确率；画图 (b)(c)。
- **先说什么**："Before I average the spread I have to say what I'm averaging over: per update, or per unit of time. They answer different questions."

**决策与 trade-off**
- **两种权重**：time weight = 下一行 ts 减这一行 ts（这个 BBO 状态持续多久）。"随机时刻下单要付多少" 用 time-weighted；忙的时候消息多、spread 也宽，所以 per-update 均值偏高（采样偏向忙的时段）。
- **Quote rule**：trade 那个 seq 对应的行就是成交**前**的 book（trade 本身不改 book）。用 `2*px` 和 `bid+ask` 比，避免半个 tick；刚好等于 mid 时 sign = 0，算"判断不了"。stale 区间里的 trade 不分类，因为那时的报价不可信。
- **100% 只说明数据干净**：这里每笔成交都打在 touch 上。真实数据有 hidden/midpoint 成交、报价和成交的时间不同步，准确率会低一些。

**关键代码**
```python
live = bbo.assign(dur_s=bbo.t.diff().shift(-1).fillna(0),            # dur_s = how long this BBO state lasted
                  spread=bbo.ask - bbo.bid)[~bbo.stale]
tr = trades.merge(live[["seq", "t", "bid", "ask"]], on="seq")       # DECISION: row at trade seq = book BEFORE it
tr["side"] = np.sign(2 * tr.px - tr.bid - tr.ask)                   # quote rule: above mid = buyer-initiated
start = (tr.t // ZOOM_S * ZOOM_S).value_counts().idxmax()           # busiest window by trade count
return live, live.spread.mean(), np.average(live.spread, weights=live.dur_s), len(tr), acc
```

**自检**：`spread event-weighted=1.63 ticks, time-weighted=1.32 ticks`；`quote rule vs truth_trades: 100.0% right on 27,687 live trades`；图 (b) 的窗口从 14:54:48 UTC 开始；图 (c) 标题是 1-tick spread 占 77% 的时间、60% 的 updates。

## 5. Step 4：Imbalance 能预测下一跳吗？（20 min）
- **目标**：imbalance 分成 decile，看 P(下一次 mid 上涨)；decile 边界只用前半段定，在后半段 out-of-sample 验证；画图 (d)。
- **先说什么**："I'll fit the decile cut-points on the first half only and use the sign of the next non-zero mid move as the target, so the second half is a genuine out-of-sample test."

**决策与 trade-off**
- target 取下一次**非零** mid 变化的方向：干净、不依赖时间尺度；代价是不知道多久以后才动，也不等于 P&L。
- decile 边界只用前半段：用全样本就是 look-ahead。
- 结论要有分寸：一部分是机械性的（排队量少的那边更容易被吃光）。能不能赚钱，要扣掉 spread、latency 和 queue position。

**关键代码**
```python
nxt = (live.bid + live.ask).diff().shift(-1).replace(0, np.nan).bfill()   # DECISION: NEXT non-zero mid move
imb = (live.bid_qty - live.ask_qty) / (live.bid_qty + live.ask_qty)
df = pd.DataFrame({"imb": imb, "up": nxt > 0})[nxt.notna()]
train, test = df.iloc[: len(df) // 2], df.iloc[len(df) // 2:]
edges = np.r_[-1, np.quantile(train.imb, np.linspace(0.1, 0.9, 9)), 1]   # DECISION: deciles from 1st half only
p_up = lambda d: d.groupby(pd.cut(d.imb, edges, labels=False, include_lowest=True)).up.mean() * 100
```

**自检**：P(up) 前半段从 21% 单调升到 79%，后半段从 23% 升到 79%；`out of sample, sign(imbalance) calls the next move 66.3%`。

## 5a. Step 5：Live mode —— reorder buffer、吞吐、并发（加分）
有时间就写（约 20 min），没时间就口头讲。面试官问"live system 怎么办"时，这一节就是答案。
- **目标**：实盘不能把整个文件排序。写一个有上限的 reorder buffer：按 seq 放行；早到的消息放进 heap 里等；缺的 seq 等了 `wait_ms` 还没来就放弃，跳过这个 gap（book 标 stale，等下一个 snapshot）。扫几个 `wait_ms`，量出"等多久"和"stale 多久"之间的 trade-off，再量吞吐。
- **先说什么**："Live, I can't see the future, so I'll hold early messages in a min-heap keyed by seq and release them in order. The only knob is how long to wait for a missing seq. Too short, and I give up on messages that were merely late, which costs staleness until the next snapshot. Too long, and every message behind a real gap waits the full budget. I'll measure that trade-off instead of guessing."

**决策与 trade-off**
- **heap 按 seq 排**：push/pop 是 O(log k)，k 是正在等的消息数。平时 k 几乎是 0，只有 0.4% 的消息需要等。重复的 seq，以及放弃之后才到的旧 seq，直接丢掉。
- **时钟**：数据里没有接收时间，所以用"见过的最新交易所 ts"当时钟。这是一个假设，要说出来。超时只在新消息到达时检查，所以市场安静时可能比预算等得久（5 ms 的预算实测最多等了 50 ms），实盘要再配一个 timer。
- **结果**：
  - 0 ms：一遇到乱序就放弃，book 在 1,800 s 里有 1,592 s 是 stale，不能用。
  - 5–20 ms：放弃了几条只是晚到的消息，stale 94 s。
  - **50 ms 起和离线排序一样**：stale 28 s，只剩真正丢失的那个 gap。
  - 200 ms：stale 也一样，但遇到真 gap 时，后面的消息最多要多等 215 ms。
  - 所以选约 50 ms（只用这一个 session 定的，要用更多天验证）；遇到 gap 立刻发 retransmit 请求，不干等 snapshot。
- **正确性**：assert 实时 book 的 live 行和离线 book 完全一致。**buffer 只会让 book 变 stale，不会让它变错。**
- **优化，先测量**：纯 Python 的 buffer 加 book 每秒处理 50 万条以上，是这份 feed 最忙一秒（418 条）的 1,000 多倍，所以 `dict + max` 完全够用，不用优化。真要优化，顺序是：
  1. cached best：只有 best level 被删掉时才重新找。
  2. tick 数组：价格换成数组下标，O(1)。
  3. C++，加上 kernel bypass。

**关键代码**
```python
        if m["seq"] >= nxt:                                   # else: duplicate, or too late (we gave up on it)
            heapq.heappush(heap, (m["seq"], j, now, m))       # j (arrival no.) breaks ties between duplicates
        while heap and (heap[0][0] <= nxt or now - min(h[2] for h in heap) >= wait_ms * 1e6):   # heap stays tiny
            seq, i, t0, m2 = heapq.heappop(heap)
            if seq >= nxt:                                    # seq > nxt: timed out, skip the gap -> book goes stale
                out.append(m2), held.append((j > i, (now - t0) / 1e6))   # (had to wait?, how long in ms)
                nxt = seq + 1
```

**并发（只讲，不写）**
- 网络线程收包，经过 lock-free SPSC ring buffer 交给 book 线程。**每个 book 只有一个写线程**（single writer），热路径上不加锁。不同 symbol 按 hash 分到不同的核（shard），线程绑核。
- 读的一方（策略）不能去锁 book。用 seqlock 或 double buffer 发布 BBO 快照，或者把 BBO 的变化当消息推出去。
- 逐单（L3）book 的增、删、改：
  - 数据结构：`order_id → 节点` 的 hash map，加上每个价位一个 FIFO 双向链表，add、cancel、reduce 都是 O(1)。
  - 改单：改小数量保留排队位置；改价或加量等于 cancel 再 new，排到队尾。
  - 内存：节点放在预分配的对象池里，热路径上不 malloc；写日志、落盘异步放到别的线程。
- Python 有 GIL，面试里讲清楚设计即可；生产里是 C++ 或 Rust。

**自检**：`wait_ms` 为 0 / 5 / 20 / 50 / 200 时，stale_s 依次是 1592.22 / 94.20 / 94.20 / 28.00 / 28.00；held_% 在 0 ms 时是 0，其余都是 0.4；max_hold_ms 依次是 0 / 49.99 / 49.99 / 60.61 / 215.09。`smallest budget as good as offline: 50 ms`。吞吐每次运行略有不同，大约每秒 50–60 万条，是最忙一秒的 1,000 多倍。

> "Live, a 50-millisecond reorder buffer gives exactly the offline book: only 0.4% of messages ever wait, and the book is never wrong, only stale. With no buffer, the book would be stale for 1,592 of 1,800 seconds. The single-threaded Python handler runs at around half a million messages a second, over a thousand times this feed's busiest second, so I wouldn't optimise it. For 5,000 symbols, I'd shard symbols across cores with one writer per book."

## 6. 图怎么讲
| 图 | 为什么这样画 | 一句英文 |
|---|---|---|
| (a) `f2_feed_health.png` | 先回答"数据能不能信"：时间序列用折线，问题时段直接涂红 | "The feed is bursty, median 57 and peak 418 messages a second, and after the seq gap the book is marked stale for 28 seconds until the next snapshot." |
| (b) `f2_bbo_trades.png` | 报价是分段常数，所以用 step；一分钟太密，放大到最忙的 3 秒；买卖用 ▲/▼ 形状区分，不靠红绿色 | "In the busiest three seconds, buyer-initiated trades print at the ask and seller-initiated ones at the bid, which is why the quote rule works here." |
| (c) `f2_spread_dist.png` | spread 是离散值，所以每个 tick 一组柱子；两种权重并排，差异一眼可见 | "A one-tick spread is 77% of the time but only 60% of updates: wide spreads come in busy bursts and close fast." |
| (d) `f2_imbalance_deciles.png` | 连续变量分桶，再看条件概率；样本内和样本外两条线，50% 虚线是 coin flip | "P(up) rises from about 21% to 79% across deciles, and the out-of-sample line sits right on top of it." |

## 7. 结论与 limitations（英文，约 45 秒）
> "I audited the feed first: one bad line, 215 duplicates, 857 late arrivals and a three-message gap. Because I had the whole file, I deduped and sorted by sequence number, and marked the book stale from the gap until the next snapshot. The live rows match the exchange's BBO 100%, while a naive arrival-order replay is wrong on 1.86% of rows and shows 2,461 crossed books, so the sequencing isn't optional. The spread is 1.32 ticks time-weighted against 1.63 per update, because wide spreads come in busy bursts. Top-of-book imbalance calls the next mid move 66% of the time out of sample."
>
> "Three limitations. Sorting only works offline. Live, the 50-millisecond reorder buffer from step 5 matches the offline book, but I'd add retransmission so a real gap doesn't cost 28 seconds. The quote rule is perfect only because every trade here prints at the touch. And next-move direction isn't P&L: it ignores spread, latency and queue position, and this is one synthetic session."

## 8. 常见坑
- 用 float 价格当 dict key，或者用 `int(px*100)` 转 tick。
- 按 ts 排序；或者把 trade 当成扣减 book 的消息，同一笔量被扣两次。
- 先排序再审计，乱序的证据就被抹掉了。
- gap 之后照样 apply、不标 stale；或者 snapshot 来了只 merge 不 reset。
- spread 只报一个均值不说口径；decile 边界用全样本算（look-ahead）。

## 9. 练习方法（三轮）
1. **第一轮**：对照这份指南一步一步写，每步写完跑一次，和"自检"的数字对上。
2. **第二轮**：只看 `problem.md` 和函数名（`load`、`replay`、`rebuild`、`market`、`imbalance`、`main`，加分项 `live_order`、`live_mode`），凭记忆写，卡住了再翻指南。
3. **第三轮**：空白文件，计时 2 小时，全程用英文出声讲：先说 policy 和 trade-off，再写代码，最后 2 分钟讲结论和 limitations。结束后对照 `solution.py`，记下漏掉的决策点。
