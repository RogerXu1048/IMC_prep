# F2 学习指南：从 JSON feed 重建 Order Book

> 用法：先读完一遍本指南，再照着 `problem.md` 从空白文件写出 `solution.py`。所有数字都来自 `python3 solution.py` 的实际输出（数据是固定 seed，每次重新生成都一样；吞吐量会因机器而异）。

## 0. 一句话题目与时间分配

**一句话**：交易所的 L2 行情（JSON lines）有重复、乱序、丢包。先把 order book 正确地重建出来，再用它算 spread、成交方向、imbalance 等微观结构统计。

| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify | 5 min | 确认语义：seq、snapshot 边界、update 是绝对量 |
| Step 1 Load + audit | 15 min | 审计报告：坏行、重复、乱序、缺号、价格精度 |
| Step 2 Rebuild | 40 min | `L2Book` + `FeedHandler`，BBO 表，和 truth 对比，naive 对比 |
| Step 3 Stats + 图 1 | 25 min | event- vs time-weighted spread、成交方向、VWAP、BBO 图 |
| Step 4 Imbalance + 图 2 | 25 min | decile 表（前半段建、后半段验证）+ 一句话结论 |
| Step 5 + 收尾 | 10 min | 性能讨论、结论陈述 |

Step 2 是核心，也是面试官最想看你"怎么做决策"的地方，时间要留足。Step 3/4 的代码都很短。

## 1. 开场 5 分钟：先问清楚，再动手

**Clarifying questions（英文，直接说）：**
- "Is the sequence number contiguous per feed, so a missing number always means a lost message?"
- "Does an `update` carry the new absolute size or a delta? That changes how dangerous a duplicate is."
- "Does a trade message change the book itself, or does the exchange publish a separate update for it?"
- "What does a snapshot's seq mean: the state before or after that seq?"
- "Can I request retransmission, or is the next snapshot my only recovery path?"
- "Who consumes the book? Is it OK to say 'stale' for a while, or do they need something every tick?"

**Assumptions to state（英文）：**
> "I'll treat the exchange sequence number as the only source of truth for ordering. I'll work in integer ticks. I'll assume there's no retransmission, so if a gap doesn't heal within a small window, I mark the book stale until the next snapshot. A stale book is better than a wrong one."

## 2. Step 1：Load + audit（15 min）

**目标**：在定任何 policy 之前，先知道数据有哪些问题。

**先说什么**：
> "Before I design the book, I want to audit the feed in arrival order: bad lines, duplicates, out-of-order messages, missing seqs, and how prices are represented. The policy in step 2 depends on what I find."

**决策点与 trade-offs**
- **逐行 `json.loads` + try/except，还是 `pd.read_json(lines=True)`？**
  选逐行。一行坏数据就会让 `read_json` 整个失败，而且真实系统本来就是逐条消息处理的。代价是比向量化慢，但 30 分钟的数据量无所谓。
- **价格的表示**：先检查 `px != round(px, 2)`，会发现浮点噪声。决定用 `round(px * 100)` 转成整数 tick。`int(px * 100)` 会截断：`100.12999999999` 变成 10012，错一个 tick。
- **审计用 arrival order**：乱序的定义就是"到达时 seq 比之前见过的最大 seq 小"。所以不能先排序再审计。

**代码骨架**
```python
def to_tick(px: float) -> int:
    return round(px * 100)          # NOT int(px * 100)

msgs, bad_lines = [], 0
with open(path) as f:
    for line in f:
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            bad_lines += 1
seqs = pd.Series([m["seq"] for m in msgs if "seq" in m])   # heartbeats have no seq
dup = seqs.duplicated()
uniq = seqs[~dup]
late = (uniq < uniq.cummax().shift(fill_value=0)).sum()  # smaller than the max seen so far
missing = sorted(set(range(uniq.min(), uniq.max() + 1)) - set(uniq))
```

**自检**（实际输出）：
- malformed lines = 1
- types：update 187,037、trade 28,187、heartbeat 1,799、snapshot 44
- duplicates = 215、arrived late = 857、missing seqs = [101534, 101535, 101536]
- 5,408 个价格带浮点噪声，其中 4,351 个用 `int(px*100)` 会错一个 tick

**可能的追问**
- *"为什么不能用 float 做 dict key？"* 100.13 和 100.13000000000001 会变成两个不同的 level，`bid >= ask` 这种比较也可能出错。生产系统里的 feed 通常本来就是定点整数。
- *"Heartbeat 有什么用？"* 没有 seq，不改变 book，但能证明链路还活着。没消息和断线是两回事。

## 3. Step 2：重建 book（40 min，核心）

**目标**：写两个组件。`L2Book` 只管价格和数量；`FeedHandler` 管消息顺序（sequencer）。输出带 `stale` 标记的 BBO 表。

**先说什么（先讲 policy，再写代码）**：
> "Policy first. I key everything on the sequence number. Anything below my next expected seq is a duplicate and gets dropped. If a message arrives early, I buffer it. If the hole doesn't fill within 20 messages, I declare a gap and mark the book stale. When a snapshot arrives with seq S, I reset the book, throw away buffered messages below S, and replay what's after it."

**决策点与 trade-offs**
- **Book 的数据结构**（面试最常问）：

  | 方案 | 更新 | best price | top-K | 备注 |
  |---|---|---|---|---|
  | dict + cached best（选它） | O(1) | O(1)；best 被删时 O(L) 重扫 | O(L log K) | 代码最短，L 只有约 50 层 |
  | heap + lazy deletion | O(log L) | O(1) | 麻烦 | 删除要做标记 |
  | sorted tree / skip list | O(log L) | O(1) | O(K) | Python 标准库没有 |
  | tick-indexed array | O(1) | 指针移动 | O(K) | 价格范围有界时 HFT 常用，cache 友好 |

  先说"I'll start with a dict plus a cached best price because it's O(1) on the common path, and only rescans when the best level is removed"，再提其他方案。
- **按 seq 还是按 timestamp 排序**：按 seq。很多消息的 timestamp 完全相同（同一笔撮合产生 trade 加 update），而且 seq 才是交易所定义的修改顺序。
- **reorder window 多大**：按消息数取 20。太小，偶尔晚到一两条就误报 gap；太大，真丢包时要很久才发现，在这期间发布的 book 是 pending 状态。也可以按时间设（比如几毫秒），实盘一般两种都设上限。
- **gap 之后怎么办**：标 stale，等 snapshot，不要"猜"。naive 做法会产生 crossed book（见自检）。
- **Snapshot 当成免费对账**：如果 book 是 live 的，并且 snapshot 正好是下一个 seq，就拿它和自己的 book 对比一下。

**代码骨架**
```python
class L2Book:
    def set(self, side, tick, qty):
        lv, best = self.levels[side], self.best[side]
        if qty > 0:                                  # absolute size, not delta
            lv[tick] = qty
            if best is None or (tick > best if side == "B" else tick < best):
                self.best[side] = tick
        else:
            lv.pop(tick, None)
            if tick == best:                         # rescan only when the best dies
                self.best[side] = (max if side == "B" else min)(lv, default=None)

class FeedHandler:
    def on_message(self, m):
        seq = m.get("seq")
        if seq is None: return                       # heartbeat
        if m["type"] == "snapshot": return self._on_snapshot(m)
        if self.next_seq is None or seq < self.next_seq or seq in self.pending:
            self.dups += 1; return                   # duplicate
        self.pending[seq] = m
        self._drain()                                # apply next_seq, next_seq+1, ... while present
        if self.pending and not self.stale and len(self.pending) > REORDER_WINDOW:
            self.gaps.append(self.next_seq); self.stale = True
```
`_on_snapshot` 做四件事：
1. 比 `next_seq` 旧的 snapshot 直接忽略。
2. 如果 book 是 live 的，用这个 snapshot 对账。
3. buffer 里所有 seq < S 的消息记为 stale 行，但 trade 仍然保留（成交确实发生了）。
4. reset book，令 `next_seq = S + 1`，再 drain 一次 buffer。

`_record` 里加一个不变量检查：`assert bid < ask`。

**怎么和 truth 比**：把 truth 的价格从美元转成 tick，按 seq merge，比较 4 列是否全部相等。naive 版本就是按到达顺序直接 apply：不去重、不缓冲、不处理 gap。

**自检**（实际输出）：
- dups dropped = 215（和 Step 1 一致）
- gap 在 seq 101534 被宣布；snapshot mismatches = 0
- stale rows = 3,464（seq 101537..105000，直到 seq 105001 的 snapshot 才恢复）
- live rows 和 truth 一致率 **100.00%**
- naive 版本有 **1.86%** 的行是错的，**2,461 行 crossed book**。原因是丢掉的那条 update 把 best ask 清空（qty=0），naive book 里留下了一个其实已经不存在的 level（phantom level）

**可能的追问**
- *"Duplicate 的 update 是绝对量，再 apply 一次不就幂等吗？"* 单独看是幂等的。但如果中间有另一条 update 改了同一个 level，晚到的 duplicate 会把它改回旧值。所以仍然要按 seq 去重。如果是 delta feed，duplicate 会直接让数量出错。
- *"1002 在宣布 gap 之后才到呢？"* `_drain` 照样能补上，然后 stale 变回 False。
- *"Snapshot 走另一个 channel 或者晚到？"* 增量消息要一直缓存（有上限），snapshot 到了以后丢掉 ≤ S 的，再 replay 后面连续的部分。如果 buffer 里最小的 seq 已经大于 S+1，就得等下一个 snapshot。

## 4. Step 3：微观结构统计（25 min）

**目标**：
- spread 分别按事件平均和按时间平均；
- 判断每笔成交是买方还是卖方主动；
- VWAP；
- 画图 1。

**先说什么**：
> "Before I average the spread, I need to decide what I'm averaging over. Per message or per unit of time? They answer different questions."

**决策点与 trade-offs**
- **Event-weighted 还是 time-weighted**：如果问题是"随机挑一个时刻下单，要付多少 spread"，用 time-weighted。如果是"每次事件发生时 spread 是多少"，用 event-weighted。本题 busy 时段消息多、spread 也宽，所以 event-weighted 会偏高。这是采样偏差（inspection paradox）。
- **每个状态持续的时间** = 下一行的 ts 减这一行的 ts。如果下一行是 stale，就设为 0，因为我们不知道期间发生了什么。
- **成交方向（trade sign）**：
  - quote rule：成交价高于 mid 算买方主动，低于 mid 算卖方主动。用"成交那个 seq 对应的行"作为成交前的报价，因为 trade 本身不改变 book。
  - 刚好等于 mid，或者处在 stale 区间时，退回 tick rule（和上一笔成交价比较）。
  - 用 `2*px` 和 `bid+ask` 比较，避免出现半个 tick 的小数。

**代码骨架**
```python
next_live = ~bbo.stale.shift(-1, fill_value=True)
bbo = bbo.assign(dur_s=(bbo.ts.shift(-1) - bbo.ts).dt.total_seconds().where(next_live, 0.0))
live = bbo[~bbo.stale]
spread = live.ask - live.bid
event_w, time_w = spread.mean(), np.average(spread, weights=live.dur_s)

t = trades.merge(bbo[["seq", "bid", "ask"]], on="seq")
t["sign"] = np.sign(2 * t.px - (t.bid + t.ask))                 # quote rule
tick_rule = np.sign(t.px.diff()).replace(0, np.nan).ffill()
t["sign"] = t.sign.where(t.sign.fillna(0) != 0, tick_rule)      # fallback
vwap = (t.px * t.qty).sum() / t.qty.sum() / 100
```
图 1：找成交最多的一分钟，bid 和 ask 用 `ax.step(..., where="post")`（价格是阶梯式变化，不能画成斜线），成交用 scatter，按方向着色。

**自检**（实际输出）：
- spread：event-weighted **1.63** ticks，time-weighted **1.32** ticks
- trades = 28,156，VWAP = $100.1235，成交方向准确率 **99.7%**
- 最忙的一分钟是 14:42 UTC

**可能的追问**
- *"Quote rule 什么时候会失效？"* 报价过时（stale）、交易所内部的 hidden 或 mid-point 成交、成交和报价的时间戳不同步。
- *"Lee-Ready 是什么？"* 就是 quote rule 加上 tick rule 作为 fallback，也就是这里的做法。

## 5. Step 4：Imbalance 能预测下一步吗？（25 min）

**目标**：
- 计算 top-of-book imbalance；
- 用前半段的数据定出 decile 边界；
- 分别看两段里每个 decile 下一次 mid 上涨的概率；
- 后半段做 out-of-sample 验证。

**先说什么**：
> "I'll fit the decile cut-points on the first half only, so the second half is a genuine out-of-sample test. The target is the sign of the next non-zero mid change, so I'm not counting the many events where the mid doesn't move."

**决策点与 trade-offs**
- **target 选什么**：可以是下一次**非零** mid 变化的方向，也可以是固定时间（比如 1 秒）后的 mid 变化。前者干净、不依赖时间尺度；后者更接近交易决策，但大部分时候 mid 不动，噪声大。
- **分箱边界只用前半段算**，否则就是 look-ahead bias。
- **结论要有分寸**：这个关系有一部分是机械性的（ask 那边排队量少，自然更容易被吃光）。能不能赚钱还要扣掉 spread 和 latency，不能直接叫 alpha。

**代码骨架**
```python
move = (live.bid + live.ask).diff().shift(-1)
df = pd.DataFrame({"imb": (live.bid_qty - live.ask_qty) / (live.bid_qty + live.ask_qty),
                   "next": np.sign(move.where(move != 0).bfill())}).dropna()
train, test = df.iloc[: len(df) // 2], df.iloc[len(df) // 2:]
edges = np.unique(np.r_[-np.inf, np.quantile(train.imb, np.linspace(0.1, 0.9, 9)), np.inf])
table = pd.DataFrame({name: d.groupby(pd.cut(d.imb, edges, labels=False)).next.apply(lambda s: (s > 0).mean())
                      for name, d in (("first_half", train), ("second_half", test))})
```

**自检**（实际输出）：
- P(next up)：前半段从 0.21 单调升到 0.79，后半段从 0.23 升到 0.79
- out-of-sample 时用 imbalance 的符号预测方向，准确率 **66.3%**（随机猜是 50%）

**可能的追问**
- *"为什么不用全样本算 decile？"* 那样测试集的信息就漏进了分箱边界。
- *"Top-5 imbalance 会更好吗？"* 可以试。需要从 book 里取 top-K level：用 `heapq.nlargest(5, levels)` 是 O(L log K)，用有序结构是 O(K)。

## 6. Step 5：性能讨论（5 min，不写新代码）

本次运行的吞吐量是 **约 1.5–1.7M msgs/s**（纯 Python，输入是已经解析好的 dict；不同机器结果不同）。
- **Hot path**：JSON 解析加上 dict 更新。JSON 解析通常和 book 更新是同一个量级甚至更慢，所以生产环境用二进制协议，做 zero-copy 解析。
- **扩展到 5,000 个 symbol**：
  - 按 symbol 或 channel 分片（shard），每个 shard 单线程顺序处理，保证结果确定，也不用加锁；
  - 价格范围有界时用 tick-indexed array 代替 dict；
  - top-K 只在有人查询时才计算；
  - 统计和存储放到旁路，比如通过 ring buffer 交给另一个线程或进程。
- **Python 的定位**：做研究、做正确性的参考实现（reference implementation）。生产热路径一般是 C++ 或 FPGA。

## 7. 收尾：怎么讲结论（英文，4–6 句）

> "The feed had one malformed line, 215 duplicates, 857 out-of-order messages and a three-message gap. With seq-based sequencing, a 20-message reorder window and stale-until-snapshot, my book matches the truth on 100% of the rows I publish, and it's honestly stale for about 3,500 messages after the gap. Applying messages in arrival order instead produces 2,461 crossed-book rows, so the integrity logic isn't optional. The time-weighted spread is 1.32 ticks against 1.63 per event, because busy periods are both wider and noisier. Top-of-book imbalance predicts the next mid move about 66% of the time out of sample. Next I'd check whether that survives the spread and our latency."

**有更多时间会做什么**：
- top-5 imbalance，以及固定时间尺度的 markout；
- 写一个 scenario test（手写 10 条消息，覆盖 dup、乱序、gap 加 snapshot）；
- 把 book 换成 tick array，测一下吞吐量。

## 8. 常见坑
- 用 float 价格当 key，或者用 `int(px*100)`。
- 按 timestamp 排序；或者把 trade 也当成扣减 book 的消息，结果同一笔量被扣两次。
- 发现 gap 后照样 apply，最后发布了 crossed book 还没察觉。
- 先排序再做审计，乱序的证据就被抹掉了。
- 平均 spread 时不说明口径（按事件还是按时间）。
- decile 边界用全样本来算（look-ahead）。
- 一上来就写复杂的类，30 分钟了还没跑通一版。正确的顺序是先写 `L2Book.set` 加最简单的循环，再把 sequencer 加上去。
- 用 `df.iterrows()` 来重建 book（极慢）。逐条消息处理就用普通 Python 循环遍历 dict 列表。

## 9. 练习方法（三轮）
1. **第一轮**：对照这份指南，一步一步写出 `solution.py`。每一步写完都运行一次，和"自检"里的数字对上。
2. **第二轮**：只看 `problem.md` 和下面的函数名，其余凭记忆写，卡住了再回来翻指南：
   `to_tick`、`step1_load_and_audit`、`L2Book.set/reset/bbo`、`FeedHandler.on_message/_drain/_on_snapshot/_record`、`naive_rebuild`、`match_rate`、`step2_rebuild`、`step3_stats`、`step4_imbalance`。
3. **第三轮**：空白文件，计时 2 小时，全程用英文出声讲：先说 policy 和 trade-off，再写代码。结束后对照 `solution.py`，记下自己漏掉的决策点。
