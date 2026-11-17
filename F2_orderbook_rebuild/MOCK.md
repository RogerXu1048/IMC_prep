# F2 Mock：Exchange feed → order book → 数据分析与画图（60 min）

这份文件分两半：
- **A**：给扮演面试官的朋友看，包括开场词、时间线、提示、追问和评分表。
- **B**：给你自己看，是答题思路和要画的图。

数据要先生成：在这个目录下运行 `python3 make_data.py`。参考图可以运行 `python3 solution.py && python3 mock_plots.py` 生成，会出现在 `../plots/f2_*.png`。

---

## A. 给面试官（朋友）

**开场原话（照读即可）**
> "We receive a price-level market-data feed for one stock, XYZ, as JSON lines: snapshots, level updates and trades, each with an exchange sequence number. It came over a real network, so messages can be duplicated, out of order, or missing. First, walk me through how you'd turn this into a correct order book. Then build it and use it to tell me what this market looked like over the 30 minutes. You can use pandas and matplotlib, and I'm happy to help with syntax."

字段说明可以直接给他看 `problem.md` 的 Data 部分。

**时间线和检查点**

| 时间 | 阶段 | 候选人应该做到 | 卡住时的提示 |
|---|---|---|---|
| 0–5 | 澄清 | 问到 L2 还是 L3、qty 是绝对值还是增量、snapshot 的 seq 语义、价格精度 | "What does `qty` mean in an update?" |
| 5–15 | 白板设计 | 画出 parser → sequencer → book → consumers 的流程，说明 gap 的处理策略 | "What if seq 1003 arrives before 1002?" |
| 15–35 | 写代码 | 稳健地读 JSONL、做数据审计、实现 book（dict 加 best）、输出 BBO 表 | "Store price as an integer number of ticks." |
| 35–50 | 分析和画图 | 至少 3 张图，每张先说为什么选这种图 | "How would you show whether the data can be trusted over time?" |
| 50–60 | 收尾和追问 | 用 4–5 句话总结，回答追问 | 从下面的追问里挑 2–3 个 |

**追问（括号里是期望答到的要点）**
1. Why not sort by timestamp? （以 seq 为准；时间戳可能相同，也可能乱序）
2. A duplicate update arrives: harmful? （qty 是绝对值，重放一次也不会改坏状态，但还是应该按 seq 丢掉）
3. How long do you wait for a missing seq? （设一个超时或缓冲上限，超过就标记 stale，等 snapshot 或请求重传；这是延迟和正确性之间的取舍）
4. Best price for 5,000 symbols, top-20 levels? （dict 加缓存的 best、heap、有序树、按 tick 索引的数组，各有取舍；按 symbol 分 shard）
5. Mean spread "per event" vs "per time": which do you report? （看用途：按时间加权反映交易者实际看到的市场，按事件加权会偏向更新频繁的时段）
6. How do you know your book is right? （和 snapshot 对账、检查 crossed book 的 invariant、和 truth 对比做回归测试）

**评分（每项 1–4 分）**

| 维度 | 4 分的样子 |
|---|---|
| 澄清和拆题 | 先问清语义再动手，主动说出自己的假设 |
| 设计和 trade-off | 流程图清楚；说得出至少 2 个取舍和理由 |
| 正确性 | 按 seq 处理、处理了重复和缺号、有 invariant 检查、做了验证 |
| 画图 | 图的选择合适（step 图、log、加权方式），标题直接写结论 |
| 沟通 | 每一步先说为什么；结论简短，并说明局限 |

---

## B. 给自己：答题思路

### 1. 澄清（English，挑 3–4 个问）
- "Is `qty` the new total at that price, or a delta?"
- "A snapshot with seq S: is that the book before or after message S?"
- "Is the sequence number per symbol or per channel? Can I request retransmission, or only wait for the next snapshot?"
- "Prices are floats. What's the tick size?"
- "Who consumes the book: a strategy that needs the best price fast, or research that needs history?"

### 2. 白板画这张图，边画边讲 30 秒

```
 Exchange L2 feed (JSON lines)         network: duplicates / reordering / loss
            │
            ▼
   ┌─────────────────┐   bad line → count it, don't crash
   │ Parser          │   px → integer ticks (round, not int)
   └────────┬────────┘
            ▼
   ┌───────────────────────────┐
   │ Sequencer                 │   next_seq, pending{seq → msg}
   │  seq <  next  → duplicate │
   │  seq == next  → apply     │
   │  seq >  next  → buffer    │   hole too long → STALE ──► wait for snapshot / ask for resend
   └────────┬──────────────────┘
            ▼   in-order messages          snapshot(S): reset book, drop buffered < S, resume
   ┌───────────────────────────┐
   │ L2Book                    │   bids{tick→qty}, asks{tick→qty}, cached best bid/ask
   │  set(side, tick, qty)     │   invariant: best_bid < best_ask
   └────────┬──────────────────┘
            ▼   BBO rows: seq, ts, bid, bid_qty, ask, ask_qty, stale
   ┌──────────────┬──────────────────┬───────────────────┐
   │ Strategy     │ Research (pandas │ Storage / replay   │
   │ best price   │ stats + plots)   │ parquet, tests     │
   └──────────────┴──────────────────┴───────────────────┘
```

> "Messages go through a sequencer before they touch the book. The exchange's sequence number defines the order, not the timestamp. Duplicates are dropped, early messages are buffered, and if a hole doesn't fill I mark the book stale rather than guess, then resync from the next snapshot. The book is two dicts keyed by integer ticks with a cached best price. Every BBO change becomes a row, which feeds both the strategy and the pandas analysis."

### 3. 关键决策和 trade-off（每条一句话说清）

| 决策 | 选择 | 代价 / 什么时候换 |
|---|---|---|
| 用什么排序 | exchange seq | 时间戳可能相同、可能乱序，只适合用来画图 |
| 价格存什么 | 整数 tick，用 `round(px*100)` | `int()` 会因为 100.1299999 少一个 tick |
| book 用什么结构 | 每边一个 dict，加缓存的 best | best 那一档被删时要 O(L) 重新扫描；层数多就换有序树或 tick 数组 |
| 缺号怎么办 | 先缓冲，超时就标 stale，等 snapshot | 牺牲约 28 秒的可用性，换来不出错；能重传的话就请求重传 |
| 重复消息 | 按 seq 丢掉 | qty 是绝对值，本来就幂等，但计数有助于监控 feed 质量 |

### 4. 代码骨架（20 分钟内写完）

```python
msgs, bad = [], 0
for line in open(path):
    try: msgs.append(json.loads(line))
    except json.JSONDecodeError: bad += 1

class L2Book:
    def __init__(self): self.lv = {"B": {}, "A": {}}; self.best = {"B": None, "A": None}
    def set(self, side, tick, qty):
        lv = self.lv[side]
        if qty > 0:
            lv[tick] = qty
            b = self.best[side]
            if b is None or (tick > b if side == "B" else tick < b): self.best[side] = tick
        else:
            lv.pop(tick, None)
            if tick == self.best[side]: self.best[side] = (max if side == "B" else min)(lv, default=None)

# sequencer: next_seq, pending dict; drain while next_seq in pending; record (seq, ts, bbo, stale)
bbo = pd.DataFrame(rows, columns=["seq", "ts", "bid", "bid_qty", "ask", "ask_qty", "stale"])
```
完整版见 `solution.py`（`L2Book` 和 `FeedHandler`）。

### 5. 画哪几张图、为什么、怎么讲

| # | 图 | 为什么选它 | 要讲的结论（数字来自参考输出） |
|---|---|---|---|
| 1 | **Feed health**：每秒消息数折线，stale 时段用阴影标出（`f2_feed_health.png`） | 先回答"数据能不能信"；时间序列用折线，问题时段直接涂出来 | "Feed is bursty, about 50 to 400 msgs/s. Seqs 101534–101536 never arrived, so the book was stale for 28 s (14:45:41–14:46:09 UTC) until the next snapshot." |
| 2 | **BBO step 图加成交散点**，取最忙的那一分钟（`f2_bbo_trades.png`） | 报价是分段常数，必须用 step；成交按主动方向分两种颜色 | "Trades print at the touch; the quote rule classifies 99.7% of them correctly." |
| 3 | **Spread 分布：按事件 vs 按时间**，每个 tick 值一组柱子（`f2_spread_dist.png`） | spread 是离散值，用柱状图；两种加权方式并排，差异一眼可见 | "1-tick spreads are 60% of updates but 77% of the time; mean 1.63 ticks per event vs 1.32 per unit time. Wide spreads come in bursts and close quickly." |
| 4 | **Imbalance 十分位 vs 下一步上涨的概率**（`f2_imbalance_deciles.png`） | 把连续变量分桶，再看条件概率；前半段建分组，后半段检验 | "P(up) rises from 0.21 to 0.79 across deciles, and out of sample the sign of imbalance calls the next move 66% of the time." |

### 6. 验证（一定要主动说）
- 非 stale 的行和 truth **100%** 一致；snapshot 对账 0 次不一致。
- 对照组：直接按到达顺序 apply，**1.86%** 的行是错的，还出现 **2,461** 行 crossed book。这说明 sequencer 是必要的。
- 数据审计结果：1 行坏 JSON，215 个重复，857 个乱序到达，3 个 seq 缺失。

### 7. 收尾（English，约 30 秒）
> "I built the book behind a sequencer, so it's correct whenever it says it's live. It matches the exchange's own BBO on 100% of live rows, while a naive arrival-order replay is wrong on about 2% of rows and even crosses. The feed lost three messages, which cost 28 seconds of staleness until the next snapshot; with a retransmission channel we could recover in milliseconds. The market itself is tight: one tick 77% of the time, and top-of-book imbalance carries real short-term signal, right about two-thirds of the time out of sample. Next, I'd add top-K depth, test it on more days, and measure throughput per symbol for a 5,000-symbol deployment."
