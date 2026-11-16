# F3 Study Guide：A/B Feed Latency，B 线升级到底有没有用？

> 用法：先读 `problem.md`，再跟着本 guide 从空白文件一步步写出 `solution.py`。
> 下面引用的数字都来自 `python3 solution.py` 的实际输出（seed 固定，可复现，运行约 3 秒）。

## 0. 一句话题目 + 时间分配

**一句话**：交易所把同一份行情从 A、B 两条线各发一遍。给你两天的抓包数据，先检查数据完整性，再算延迟、做 A/B arbitration，最后回答"网络团队说 B 线升级后更快了，是真的吗？"

| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify + 说计划 | 5 min | 问清问题，说出 5 步计划 |
| Step 1 完整性检查 | 20 min | 每条线、每天的 integrity 表；两条线同时丢包的 seq |
| Step 2 单线延迟 | 20 min | 分位数表 + CCDF 图 + 时钟漂移的论证 |
| Step 3 Arbitration | 25 min | 胜率表、arbitrated 分位数，online arbiter |
| Step 4 升级有没有用 | 30 min | 天真对比，然后 paired + 按负载分层；knee |
| Step 5 结论 | 10 min | 3–5 句给网络团队的话 |
| 机动 / 追问 | 10 min | |

**核心思路**：两条线运的是**同一批包**，这是整题最重要的结构。利用它做 paired 对比，可以消掉流量、交易所和时钟三方面的差异。

---

## 1. 开场 5 分钟：先问，再说假设

**Clarifying questions（英文，挑 3–4 个问）：**
- "Is `seq` contiguous per day, so a missing seq on both lines means the message was really lost upstream?"
- "Are `rx_ns` hardware NIC timestamps, and are both capture NICs on the same PTP clock?"
- "Is `exch_ns` stamped when the exchange sends, before or after their own queueing?"
- "Is a 'faster line' judged by median, tail, or by how often it wins the race? What does the strategy care about?"
- "Were there other changes between the two days, like traffic, exchange releases, or our own software?"

**Assumptions to state：**
> "I'll assume each capture is complete, so a missing row means our NIC never got the packet. I'll treat `rx − exch` as latency plus an unknown clock offset, so I'll trust differences measured on our clock more than absolute values. And I'll judge the upgrade on the same packets across lines wherever I can."

**说计划（30 秒）：**
> "First I'll check integrity: duplicates, ordering and loss per line. Then per-line latency percentiles. Then the arbitration logic, both as an offline merge and as the online algorithm a feed handler runs. Then the upgrade question, where I expect confounding between the two days. And I'll finish with a short recommendation."

---

## 2. Step 1：加载 + 完整性检查

**目标**：对每条线、每天，数清 packets、duplicates、out-of-order 和丢包数；找出两条线**同时**丢的 seq。

**先说什么：**
> "Before computing any latency I want to know what's wrong with the data. Duplicates would double-count, and loss on both lines is a correctness problem for the book, not just a latency problem."

**决策点与 trade-offs：**
- **Duplicate 保留哪一份？** 保留第一份（`keep="first"`）。重复的那份不带任何新信息，而且它的 rx 更晚，留着会把延迟拉高。
- **怎么判断乱序？** 文件是按到达顺序排的，某个 seq 小于"到目前为止见过的最大 seq"，就是乱序到达，用 `cummax`，O(n)。另一种做法是排序后比较，但那样会丢掉"到达顺序"这个信息。
- **丢包的 universe 怎么定？** 用 1..max(seq)。**Trade-off**：如果当天最后几个 seq 两条线都丢了，这个方法看不出来。要说出这个 edge case。
- **时间戳用什么类型？** 先减去当天零点，变成 day-relative ns。epoch ns 约 1.8e18，大于 2^53。之后 outer merge 时 int64 遇到 NaN 会被**静默转成 float64**，精度丢到 256 ns。这是一个面试官很喜欢的细节。

**代码骨架：**
```python
raw = pd.read_parquet(DATA / f"feed_{line}.parquet")      # arrival order
raw["date"] = raw["date"].astype(str)
raw["dup"] = raw.duplicated(["date", "seq"])
df = raw[~raw["dup"]].copy()                               # DECISION: keep first copy
df["ooo"] = df["seq"] < df.groupby("date")["seq"].cummax() # out-of-order arrival
day0 = pd.to_datetime(df["date"]).astype("datetime64[ns]").astype("int64")
df["exch"], df["rx"] = df["exch_ns"] - day0, df["rx_ns"] - day0
df["lat_us"] = (df["rx_ns"] - df["exch_ns"]) / 1e3

# loss: set difference against 1..max seq; runs = clustered vs random
universe = set(range(1, max(max(s) for s in got.values()) + 1))
lost = sorted(universe - got[line])
mean_run = len(lost) / runs(lost)          # runs = 1 + (np.diff(lost) != 1).sum()
both_lost = sorted((universe - got["A"]) & (universe - got["B"]))
```

**自检（应该看到）：**
- A 第 2 天有 **25** 个 duplicate；B 第 1 天有 **15** 个乱序。
- 丢包：A 两天都约 **0.054–0.055%**，`mean_run` 约 1.05，说明是随机丢包。B 第 1 天 **0.021%**，第 2 天 **0.213%**（1018 个），`mean_run` 约 1.5–1.7，说明是**成簇**丢的。
- 两条线同时丢：第 1 天 **10 个 seq / 3 段**，第 2 天 **22 个 seq / 4 段**。

**可能的追问：**
- *"两条线都丢了怎么办？"* 这是上游丢的（A/B 分叉之前），要向交易所的 retransmission server 请求重发，或者等 snapshot 重建 book。期间 book 标 stale，策略不能用。
- *"成簇丢包说明什么？"* B 的 buffer 浅，在 microburst 时 tail drop。Step 3 会用数据证明。

---

## 3. Step 2：单线延迟 + 绝对值能不能信

**目标**：每条线、每天的 p50/p90/p99/p99.9；一张能看清尾部的图；论证"绝对的 one-way latency 不可信，但 A 与 B 之间的差值可信"。

**先说什么：**
> "Latency is heavy-tailed, so I'll report percentiles and plot a CCDF on log-log axes, otherwise the tail is invisible. Then I want to check whether `rx − exch` is even a real number."

**决策点与 trade-offs：**
- **画什么图？** CCDF（P(latency > x)）用 log-log 坐标，尾部一目了然。直方图用线性坐标会把尾部压成看不见的一条线。**Trade-off**：CCDF 对非技术听众不太直观，汇报时可以换成"p99 柱状图"。
- **怎么证明时钟问题？** 两个不同的时钟算出来的 one-way latency = 真实延迟 + offset(t)。**按分钟取最小值**会把排队和噪声都去掉，只剩"物理最短路径 + 时钟 offset"。如果两条**独立**的线、两天都出现**一样的**漂移，那漂的就是时钟，不是网络。

**代码骨架：**
```python
lat = pd.concat([f.assign(line=l) for l, f in feeds.items()], ignore_index=True)
print(lat.groupby(["line", "date"])["lat_us"].quantile(QS).unstack().round(1))

lat["minute"] = lat["exch"] // 60_000_000_000
mins = lat.groupby(["line", "date", "minute"])["lat_us"].min().reset_index()
for (line, date), g in mins.groupby(["line", "date"]):
    slope = np.polyfit(g["minute"], g["lat_us"], 1)[0] * 60     # us per hour

# CCDF: sort, then plot 1 - rank/n on log-log axes
x = np.sort(g["lat_us"].to_numpy()); ax.plot(x, 1 - np.arange(len(x)) / len(x))
```

**自检：**
- 分位数（µs）：A 第 1 天 24.6 / 29.8 / 65.7 / 148.3；B 第 1 天 27.4 / 36.2 / 69.6 / 146.6；A 第 2 天 25.2 / 41.0 / 124.2 / 490.2；B 第 2 天 24.0 / 39.5 / 110.7 / 409.8。
- 每分钟最小值的漂移：四组都是 **+1.00 到 +1.02 µs/hour**，完全一样，所以是时钟漂移（2 小时漂了约 2 µs）。
- 图 `f3_latency_ccdf.png`：第 2 天两条线的尾巴都明显更长。

**可能的追问：**
- *"时钟怎么同步？"* 两边都用 PTP 对齐到 GPS 或 grandmaster，配合硬件时间戳可以到几十 ns–1 µs 的精度。NTP 只有 ms 级，不能用来测 µs 延迟。
- *"硬件和软件时间戳的区别？"* 硬件时间戳由 NIC 在包到达时打，软件时间戳会混进中断、softirq 和调度的抖动（µs 级）。
- *"p99 能跨天求平均吗？"* 不能。分位数不可加，要合并原始数据或者可合并的 sketch（HDR histogram）。

---

## 4. Step 3：A/B Arbitration（算法核心）

**目标**：(a) 离线版：按 seq 合并两条线，先到的赢，算出胜率和 arbitrated 分位数。(b) 在线版：feed handler 实际运行的算法，结果必须和离线版一致。

**先说什么：**
> "Offline, arbitration is just an outer join on seq, taking the earlier arrival. Online, it's a small state machine: process packets in arrival order, publish strictly in seq order, drop the second copy, and if a seq is missing on both lines for longer than a timeout, declare a gap and request a retransmission."

**决策点与 trade-offs：**
- **merge 用 outer。** inner 会丢掉"只有一条线收到"的包，而那正是 arbitration 价值最大的地方。
- **用 `min(axis=1)`**：它会跳过 NaN，所以丢了包的线自然不会赢。平局（rx 完全相等）算 A 赢，并说明平局极少。
- **Invariant**：两条线上同一个 seq 的 `exch` 必须相同。用 `assert` 把它写死，这是"同一批包"这个假设的守门员。
- **在线版的数据结构：**
  - `heapq.merge`：两条线各自已按到达时间排好，合并起来的复杂度是 O(n log 2)。这就是 NIC 看到的顺序。
  - `next_seq` + `pending` dict：已发布的 seq 用 `seq < next_seq` 判断，O(1)；提前到的包放进 pending 等前面的 seq 补齐。
  - **gap timeout 的 trade-off**：设短了，另一条线只是慢一点也会被误判成 gap，触发不必要的重传请求；设长了，book 被冻住的时间更长，策略在等。可以根据 |A−B| 到达差的分布来定，比如取 p99.9 再加余量。这里用的是 100 µs。
- **复杂度**：每个包均摊 O(1)。pending 的大小受"gap 持续时间 × 消息率"限制。

**代码骨架（离线）：**
```python
m = feeds["A"][cols].merge(feeds["B"][cols], on=["date", "seq"], how="outer", suffixes=("_A", "_B"))
same = m.dropna(subset=["exch_A", "exch_B"])
assert (same["exch_A"] == same["exch_B"]).all()          # CHECK: same packets on both lines
m["exch"] = m["exch_A"].fillna(m["exch_B"])
m["rx_arb"] = m[["rx_A", "rx_B"]].min(axis=1)              # first arrival wins, NaN-aware
m["winner"] = np.select([m["rx_B"].isna(), m["rx_A"].isna(), m["rx_A"] <= m["rx_B"]],
                        ["A only", "B only", "A"], "B")
```

**代码骨架（在线）：**
```python
arrivals = heapq.merge(zip(a["rx"].tolist(), a["seq"].tolist(), repeat("A")),
                       zip(b["rx"].tolist(), b["seq"].tolist(), repeat("B")))
next_seq, pending, wins, gaps = 1, {}, {"A": 0, "B": 0}, []
for rx, seq, line in arrivals:
    if seq < next_seq or seq in pending:
        continue                                   # other line already delivered it
    wins[line] += 1
    pending[seq] = rx
    while pending:
        if next_seq in pending:                    # publish in order
            del pending[next_seq]; next_seq += 1
        elif rx - min(pending.values()) > GAP_TIMEOUT_NS:
            first = min(pending)
            gaps.append((next_seq, first - 1))     # -> request retransmission
            next_seq = first
        else:
            break
```

**自检：**
- 胜率：第 1 天 A 赢 **98.86%**（另有 A only 0.02%）；第 2 天 B 赢 **94.75%**（另有 B only 0.05%）。
- 第 2 天 arbitrated 的 p99 是 **122.7 µs，比 B 单线的 110.7 µs 还高**。这是 survivorship bias：B 丢掉的恰好是深队列里最慢的那些包（它们在 A 上的中位延迟 **253 µs**，全体只有 25 µs），arbitration 从 A 把它们补回来，样本里就多了这些慢包。
- 在线版：第 1 天 A 288608 / B 3282，3 个 gap / 10 个 seq；第 2 天 A 24792 / B 452074，4 个 gap / 22 个 seq。与离线版**完全一致**（代码里有 assert）。

**可能的追问：**
- *"为什么不用 TCP？"* 行情是一对多，要用 multicast。TCP 丢包后要重传，在重传完成前后面的数据都交不上去（head-of-line blocking），延迟会跳到 ms 级。A/B 冗余加 UDP 避免了这个问题。
- *"timeout 期间，下游怎么办？"* 先标 stale，不暂停接收。新包继续进 pending，等 gap 补上或 snapshot 到了再追上。
- *"单线的 p99 能比 arbitrated 好吗？"* 能，见上面的 survivorship。公平的做法是在同一批 seq 上比较，或者把丢包当作延迟 = ∞。

---

## 5. Step 4：升级到底有没有用？（统计判断核心）

**目标**：先做天真的前后对比，发现矛盾，找到 confounder，再用 paired + 分层给出公平结论；顺便找出 capacity knee。

**先说什么：**
> "The naive answer is to compare B's latency on day 1 versus day 2. But traffic differs between days, so I'll use line A as a control, check whether load changed, and then compare B and A on the same packets, which cancels traffic, the exchange side, and the clock."

**决策点与 trade-offs：**
- **负载怎么量化？** 用"过去 100 µs 交易所发了多少条"。在按时间排好序的 exch 上做 **two pointers**；用 `np.searchsorted` 向量化，O(n log n)，几十万行不到一秒。纯 Python 双指针是 O(n)，但常数大得多。
- **负载用哪条线算？** 用两条线的**并集**。如果只用 B 自己的包，B 丢掉的包也不会被计入负载，负载就被低估了。
- **公平比较的三档：**
  1. 天真对比：直接比 B 第 1 天和第 2 天，会被流量差异误导。
  2. 按负载分层：每个负载档位内比较。好一些，但"过去 100 µs 的消息数"只是队列深度的近似，高负载档位里仍有残余 confounding。
  3. **Paired**：同一个包的 B − A。交易所、流量组合、时钟全部抵消，这是最强的设计。
- **显著性**：样本有几十万，KS 检验几乎一定 p≈0，没有信息量。要报告 effect size（差了多少 µs）。如果要做置信区间，iid bootstrap 不对：同一个 burst 里的包强相关。要用 **block bootstrap**（比如按 1 秒分块），或者先聚合到每秒的统计量再做推断。可以口头说明，不一定写代码。

**代码骨架：**
```python
m = m.sort_values(["date", "exch"])
m["rate"] = m.groupby("date")["exch"].transform(
    lambda e: np.arange(len(e)) - np.searchsorted(e, e - RATE_WINDOW_NS, side="right") + 1)

naive = m.groupby("date")[["lat_A", "lat_B"]].quantile([0.5, 0.99]).unstack()
burst_share = (m["rate"] >= 100).groupby(m["date"]).mean() * 100

m["bucket"] = pd.cut(m["rate"], RATE_BUCKETS, right=False)
both = m.dropna(subset=["lat_A", "lat_B"])
diff = both["lat_B"] - both["lat_A"]                       # paired, same packet
by_load = diff.groupby([both["bucket"], both["date"]], observed=True).median().unstack()

# knee: first load bin after which line A's median stays > idle baseline + 2 us
a = long[long["line"] == "lat_A"].groupby("rbin")["lat"].median()
above = a > a.iloc[:2].mean() + 2
knee = int(a.index[a.index > a.index[~above].max()].min())
```

**自检：**
- 天真对比：B 的 p50 从 27.4 降到 24.0（变好），但 **p99 从 69.6 升到 110.7**（变差）。没有改动的 A，p99 也从 65.7 升到 124.2，所以第 2 天本身就不一样。
- 流量：≥100 msgs/100µs 的消息占比 **1.7% → 5.1%**（3.1 倍），第 2 天 microburst 多得多。
- Paired 中位数 B − A：第 1 天每个负载档位都是 +2.3 到 +4.6 µs，第 2 天每个档位都约 **−1.2 µs**。整体从 **+2.5 变为 −1.2，B 快了约 3.7 µs**。
- Knee：约 **75 msgs/100µs**。1G 链路每条消息约 122 B（payload + 62 B 开销），容量约 **102 msgs/100µs**，所以排队从约 **73%** 利用率开始。
- 图 `f3_latency_vs_rate.png`：低负载时 B 第 2 天的线最低；超过 knee 后四条线一起陡升。

**可能的追问：**
- *"为什么利用率不到 100% 延迟就爆了？"* 排队论：M/M/1 的等待时间正比于 ρ/(1−ρ)，ρ=0.8 时约 4 个服务时间，ρ=0.95 时约 19 个。而且 burst 内的瞬时利用率远高于平均值。
- *"所以该怎么修尾部？"* 升级交换机只是把整条曲线平移了约 3.7 µs，要压尾部得**加带宽**（10G/25G），或者把流量分到更多 multicast group。
- *"还有哪些 confounder？"* 时段、消息类型和包大小的组合、交易所侧的发布、我方接收端的改动、PTP 状态。Paired 设计能消掉其中大部分。

---

## 6. Step 5：结论（收尾 10 分钟）

**English script（4–6 句，直接对网络团队说）：**
> "Yes, the upgrade worked. On the same packets, line B went from about 2.5 µs slower than A to 1.2 µs faster, at every load level, and B now wins 95% of the races. The scary-looking p99 increase on B isn't the switch: day two had about three times more microburst traffic, and line A, which nobody touched, got worse too. The tail is a capacity problem. Both lines start queueing at around 75 messages per 100 µs, roughly three quarters of a 1-gig link, so the next win is more bandwidth, not a faster switch. Separately, 32 sequence numbers were lost on both lines, so our retransmission and snapshot recovery path has to stay healthy."

**如果有更多时间：**
- 用 block bootstrap 给"B 快了 3.7 µs"加一个置信区间。
- 做 difference-in-differences：(B 第 2 天 − B 第 1 天) − (A 第 2 天 − A 第 1 天)。
- 按 msg_type / 包大小分组，看大包是否更慢（serialization delay）。
- 把 gap timeout 设成 |A−B| 分布的 p99.9，并模拟不同 timeout 下误报 gap 的数量。

---

## 7. 常见坑
1. 只报 mean，或者用线性坐标的直方图，尾部看不见。
2. 把 `rx − exch` 当成真实物理延迟直接下结论，没有提时钟 offset 和漂移。
3. 忘了 dedupe：duplicate 会把延迟统计拉偏，也会把丢包算少。
4. outer merge 后 epoch ns 变成 float64，精度丢失，出现大量假平局。
5. 看到 B 的 p99 变差就说"升级让尾部变差了"，没有用 A 当 control。
6. 只用 B 自己的包算负载，B 丢掉的包没被计入，负载被低估。
7. 用 KS 的 p 值代替 effect size；用 iid bootstrap 却不提自相关。
8. 没提"两条线都丢"的 seq 需要恢复。这是正确性问题，比延迟更重要。

---

## 8. 练习方法（三轮）
- **Round 1：跟着 guide 写。** 每一步先读"先说什么"并大声说一遍，再敲代码骨架，跑通后对照"自检"的数字。目标是理解每个 DECISION 和 TRADE-OFF。
- **Round 2：只看 `problem.md` 和函数名。** 新建空文件，只写下 `step1_load_and_check`、`step2_latency`、`step3_arbitrate`、`step3b_live_arbiter`、`step4_upgrade`、`step5_conclusion` 这几个函数签名，其他全部自己写。卡住超过 5 分钟再看 guide 对应的那一节。
- **Round 3：空白文件，2 小时计时，全程用英文 talk aloud。** 先 clarify 5 分钟，每一步写之前先说 decision。结束时用 4–6 句英文做结论。录音回听，检查有没有"沉默写代码超过 2 分钟"的地方。
