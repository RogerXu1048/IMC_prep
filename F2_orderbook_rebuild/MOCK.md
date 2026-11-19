# F2 Mock：Exchange feed → order book → 数据分析与画图（60 min）

这份文件分两半：
- **A**：给扮演面试官的朋友看，包括开场词、时间线、提示、追问和评分表。
- **B**：给你自己看，是答题思路和要画的图。

数据要先生成：在这个目录下运行 `python3 make_data.py`。参考输出和 4 张图：运行 `python3 solution.py`，图会出现在 `../plots/f2_*.png`。

---

## A. 给面试官（朋友）

**开场原话（照读即可）**
> "We receive a price-level market-data feed for one stock, XYZ, as JSON lines: snapshots, level updates and trades, each with an exchange sequence number. It came over a real network, so messages can be duplicated, out of order, or missing. First, walk me through how you'd turn this into a correct order book. Then build it and use it to tell me what this market looked like over the 30 minutes. You can use pandas and matplotlib, and I'm happy to help with syntax."

字段说明可以直接给他看 `problem.md` 的 Data 部分。

**时间线和检查点**

| 时间 | 阶段 | 候选人应该做到 | 卡住时的提示 |
|---|---|---|---|
| 0–5 | 澄清 | 问到 qty 是绝对值还是增量、snapshot 的 seq 语义、价格精度、离线还是实时 | "What does `qty` mean in an update?" |
| 5–15 | 白板设计 | 画出 parser → 去重排序 → book → 分析的流程，说明 gap 的处理策略 | "What if seq 1003 arrives before 1002?" |
| 15–35 | 写代码 | 稳健地读 JSONL、做审计、按 seq 去重排序、dict book、输出带 `stale` 的 BBO 表并和 truth 对比 | "You have the whole file. Do you need a streaming reorder buffer?" |
| 35–50 | 分析和画图 | 至少 3 张图，每张先说为什么选这种图 | "How would you show whether the data can be trusted over time?" |
| 50–60 | 收尾和追问 | 用 4–5 句话总结，主动说 limitations，回答追问 | 从下面的追问里挑 2–3 个 |

**追问（括号里是期望答到的要点）**
1. Why not sort by timestamp? （以 seq 为准；很多消息 ts 相同，ts 只适合画图）
2. You sorted the whole file. What changes in a live system? （不能看到未来：用 heap 做有上限的 reorder buffer 加 timeout，超时就跳过 gap、标 stale，等 snapshot 或请求重传。这是延迟和可用性之间的取舍，要用数据定预算：0 ms 时 book 1,800 s 里有 1,592 s stale；50 ms 起和离线一样；只有 0.4% 的消息需要等）
3. A duplicate update arrives: harmful? （qty 是绝对值，单独重放一次无害；但如果中间有别的 update 改过这个 level，晚到的重复会把它改回旧值，所以还是按 seq 丢掉）
4. Best price for 5,000 symbols, top-20 levels? （dict 加 `max`、cached best、heap、sorted tree、tick-indexed array 各有取舍；按 symbol 分 shard）
5. Mean spread "per event" vs "per time": which do you report? （看用途：time-weighted 是随机时刻交易者实际付的；per-event 偏向更新频繁的忙时段）
6. How do you know your book is right? （和 truth 逐行对比、`bid < ask` 的 invariant、naive 对照组、和 snapshot 对账、写回归测试）
7. Is your Python fast enough? Where would you optimise first? （先测量：约 50 万条/秒，是最忙一秒的 1,000 多倍，所以不用优化；真要优化，按顺序是 cached best → tick 数组 → C++）
8. Multi-core, 5,000 symbols, order-by-order feed: how do add, cancel and modify work without locks? （按 symbol 分 shard，每个 book 只有一个写线程；网络线程经 SPSC ring 交给它；`order_id → 节点` 的 hash map 加每个价位一个 FIFO 链表，增删改都是 O(1)；改小数量保留排队位置，改价或加量 = cancel + new；读方用 seqlock 或快照；对象池，不在热路径上 malloc）

**评分（每项 1–4 分）**

| 维度 | 4 分的样子 |
|---|---|
| 澄清和拆题 | 先问清语义再动手，主动说出自己的假设（比如"离线，所以可以排序"） |
| 设计和 trade-off | 流程图清楚；说得出至少 2 个取舍和理由（排序 vs buffer、dict + max vs 有序结构） |
| 正确性 | 按 seq 去重排序、gap 标 stale 到下一个 snapshot、有 invariant 检查、和 truth 对比 |
| 画图 | 图的选择合适（step 图、离散值用柱状、两种加权并排），标题直接写结论，坐标轴带单位 |
| 沟通 | 每一步先说为什么；结论简短，并说明局限 |

---

## B. 给自己：答题思路

### 1. 澄清（English，挑 3–4 个问）
- "Is `qty` the new total at that price, or a delta?"
- "A snapshot with seq S: is that the book before or after message S?"
- "Is this an offline analysis of the full file, or should I design it like a live handler?"
- "Prices are floats. What's the tick size?"
- "Who consumes the book: a strategy that needs the best price fast, or research that needs history?"

### 2. 白板画这张图，边画边讲 30 秒

```
 Exchange L2 feed (JSON lines)          network: duplicates / reordering / loss
            │
            ▼
   ┌────────────────────────┐   bad line → count it, don't crash
   │ Parser + audit         │   px → integer ticks (round, not int)
   └───────────┬────────────┘   audit in ARRIVAL order: dups, late, missing seqs
               ▼
   ┌────────────────────────┐   offline: the whole file is on disk
   │ Dedupe + sort by seq   │   (live: bounded reorder buffer + timeout instead)
   └───────────┬────────────┘
               ▼   messages in seq order
   ┌────────────────────────────┐
   │ replay(): L2 book          │   bids{tick→qty}, asks{tick→qty}, best = max / min
   │  snapshot(S) → reset book  │   update qty = new absolute size, 0 = delete
   │  seq gap     → STALE       │   ... until the next snapshot
   └───────────┬────────────────┘
               ▼   BBO rows: seq, ts, bid, bid_qty, ask, ask_qty, stale
   ┌────────────────┬──────────────────┬────────────────────┐
   │ Check vs truth │ Market stats     │ Imbalance signal    │
   │ + naive replay │ spread, sides    │ deciles, out of     │
   │ (bid < ask)    │ (quote rule)     │ sample              │
   └────────────────┴──────────────────┴────────────────────┘
```

> "Because I have the whole file offline, I dedupe by sequence number and sort by it; the exchange's seq defines the order, not the timestamp. Then I replay the messages into two dicts of tick to size: a snapshot resets the book, an update sets the absolute size, and zero deletes the level. If the seq jumps, I mark the book stale until the next snapshot rather than guess. Every message becomes a BBO row, which I check against the truth and then use for the analysis. In a live system I'd replace the sort with a bounded reorder buffer and a timeout."

### 3. 关键决策和 trade-off（每条一句话说清）

| 决策 | 选择 | 代价 / 什么时候换 |
|---|---|---|
| 用什么排序 | exchange seq；离线直接去重 + 排序 | 时间戳会重复，只适合画图；实盘要 reorder buffer + timeout |
| 价格存什么 | 整数 tick，用 `round(px*100)` | `int()` 会把 100.12999999999 截成 10012，少一个 tick |
| book 用什么结构 | 每边一个 dict，best 用 `max`/`min` | 每条消息 O(levels)，约 50 档没问题；层数多或要求低延迟就换 cached best、有序树或 tick 数组 |
| 缺号怎么办 | 标 stale，等下一个 snapshot | 牺牲 28 秒的可用性，换来不出错；能重传的话就请求重传 |
| 重复消息 | 按 seq 去掉 | qty 是绝对值，单独重放无害，但晚到的重复会覆盖更新的值 |
| 成交方向 | quote rule，用 trade 那个 seq 的行（成交前的 book） | 这里 100% 是因为成交都在 touch；真实数据有 hidden/midpoint 成交 |
| 实盘的排序 | heap 做 reorder buffer，等缺号最多 50 ms | 预算太短，只是晚到的消息也被放弃，book 变 stale；太长，真 gap 后面的消息都要多等。用数据扫出拐点 |
| 并发 | 按 symbol 分 shard，每个 book 一个写线程 | 热路径无锁；代价是一个很热的 symbol 不能再拆，只能给它单独一个核 |

### 4. 代码骨架（20 分钟内写完）

```python
msgs, bad = [], 0
for line in open(DATA / "feed.jsonl"):
    try:
        msgs.append(json.loads(line))
    except json.JSONDecodeError:
        bad += 1

def replay(msgs):                              # apply msgs in the order given -> one BBO row per msg
    book, rows, prev, stale = {"B": {}, "A": {}}, [], 0, False
    for m in msgs:
        if m["type"] == "snapshot":            # snapshot seq S = book after all seq < S -> full reset
            book = {"B": {tick(p): q for p, q in m["bids"]}, "A": {tick(p): q for p, q in m["asks"]}}
        elif m["type"] == "update":            # qty = new absolute size, 0 = delete (trades don't touch the book)
            if m["qty"] > 0:
                book[m["side"]][tick(m["px"])] = m["qty"]
            else:
                book[m["side"]].pop(tick(m["px"]), None)
        stale = m["type"] != "snapshot" and (stale or m["seq"] != prev + 1)   # seq gap -> stale until snapshot
        prev, b, a = m["seq"], max(book["B"]), min(book["A"])
        rows.append((m["seq"], m["ts"], b, book["B"][b], a, book["A"][a], stale))
    return pd.DataFrame(rows, columns=["seq", "ts", *COLS, "stale"])

ordered = sorted({m["seq"]: m for m in msgs if "seq" in m}.values(), key=lambda m: m["seq"])
bbo = replay(ordered)                                            # correct book
naive = replay([m for m in msgs if "seq" in m]).drop_duplicates("seq", keep="last")   # arrival order: control group
```
完整版见 `solution.py`（`load`、`replay`、`rebuild`、`market`、`imbalance`、`main`；加分的 Step 5 是 `live_order`、`live_mode`）。

### 5. 画哪几张图、为什么、怎么讲

| # | 图 | 为什么选它 | 要讲的结论（数字来自参考输出） |
|---|---|---|---|
| 1 | **Feed health**：每秒消息数折线，stale 时段涂红（`f2_feed_health.png`） | 先回答"数据能不能信"；时间序列用折线，问题时段直接涂出来 | "Feed is bursty, median 57 and peak 418 msgs/s. Seqs 101534–101536 never arrived, so the book was stale for 28 s (14:45:41–14:46:09 UTC) until the next snapshot." |
| 2 | **BBO step 图加成交散点**，取最忙的 3 秒（`f2_bbo_trades.png`） | 报价是分段常数，必须用 step；一分钟的点太密，所以放大到几秒；成交方向用 ▲/▼ 形状区分，不靠红绿色 | "Buys print at the ask and sells at the bid; the quote rule gets 100.0% of 27,687 live trades right, because every trade here prints at the touch." |
| 3 | **Spread 分布：按事件 vs 按时间**，每个 tick 值一组柱子（`f2_spread_dist.png`） | spread 是离散值，用柱状图；两种加权方式并排，差异一眼可见 | "1-tick spreads are 60% of updates but 77% of the time; mean 1.63 ticks per event vs 1.32 per unit time. Wide spreads come in bursts and close quickly." |
| 4 | **Imbalance 十分位 vs 下一步上涨的概率**（`f2_imbalance_deciles.png`） | 把连续变量分桶，再看条件概率；前半段建分组，后半段检验 | "P(up) rises from 21% to 79% across deciles, and out of sample the sign of imbalance calls the next move 66.3% of the time." |

### 6. 验证（一定要主动说）
- 非 stale 的行和 truth **100.00%** 一致；live 行里没有一行 `bid >= ask`（assert）。
- 对照组：直接按到达顺序 replay，**1.86%** 的行是错的，还出现 **2,461** 行 crossed book。大部分出现在 gap 之外，是乱序和重复把删掉的 level "复活"造成的。这说明按 seq 处理是必要的。
- 数据审计结果：1 行坏 JSON，215 个重复，857 个乱序到达，3 个 seq 缺失（101534–101536），3,464 行标为 stale。
- 实时模式（Step 5）：reorder buffer 输出的 live 行和离线 book 完全一致（assert），所以 buffer 只会让 book 变 stale，不会让它变错。

### 7. 收尾（English，约 30 秒）
> "I built the book by deduping and sorting on the exchange sequence number, so it's correct whenever it says it's live: it matches the exchange's BBO on 100% of live rows, while a naive arrival-order replay is wrong on 1.86% of rows and even crosses. The feed lost three messages, which cost 28 seconds of staleness until the next snapshot. The market itself is tight: one tick 77% of the time, and top-of-book imbalance calls the next mid move about two-thirds of the time out of sample. Sorting only works offline, but live, a 50-millisecond reorder buffer reproduces the same book while only 0.4% of messages wait; I'd add a retransmission request so a real gap doesn't cost 28 seconds. Next, I'd test the signal against the spread and our latency on more days."
