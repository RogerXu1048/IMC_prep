# P04 参考答案：A/B Feed Arbitration 与 "升级变快了吗？"

> 先自己做完再看。数字来自 `python3 solution.py` 的输出（seed 固定，可复现）。

---

## 1. 预设的 findings 以及怎么发现它们

| # | 预设 finding | 怎么发现 |
|---|---|---|
| 1 | **数据质量**：A 第 2 天有 25 个 duplicate packet（镜像端口故障）；B 第 1 天有 15 对相邻乱序（rx 顺序 ≠ seq 顺序） | `duplicated(["date","seq"])`；按 rx 顺序看 `seq < cummax(seq)` |
| 2 | **丢包**：A 约 0.05%、随机（iid）；B 第 1 天 0.02%、第 2 天 0.21%，**成簇（clustered）**，而且全发生在 microburst 里；另有 7 小段（3–6 个连续 seq，共 32 个）**两条线同时丢**（上游丢的） | universe = 1..max(seq)，做集合差；数 run（连续段）个数：B 的 run 平均长度 > 1.5，A 的约等于 1。B 丢掉的包在 A 上的 latency 中位数约 250 µs，整体只有 25 µs，说明 B 只在深队列时丢包（浅 buffer 发生 tail drop） |
| 3 | **时钟漂移**：`rx_ns - exch_ns` 里包含两边时钟的 offset，约 +3 µs，而且 2 小时内线性漂移约 +2 µs | 按分钟取 **minimum latency**。最小值把排队和噪声都去掉了，剩下的是物理传播时间加上时钟 offset。A、B 两条线、两天的斜率都是约 +1 µs/h，**完全一样**，所以这是时钟的问题，不是网络的问题 |
| 4 | **Arbitration**：第 1 天 A 赢 98.9%，第 2 天 B 赢 94.8% | outer merge on (date, seq)，对 `rx_A`、`rx_B` 取 min |
| 5 | **Simpson's paradox**：天真地比较，B 的 p50 从 27.4 降到 24.0 µs，**但 p99 从 69.7 升到 110.7 µs**，看起来升级把尾部搞坏了。其实第 2 天是波动大的一天，消息量多 1.6 倍，落在 ≥100 msgs/100µs 的消息占比是第 1 天的约 3 倍。没升级的 A 线 p99 也从 65.7 升到 124.2 µs | 拿 A 当 **control**；按 rate bucket 分层；最干净的做法是 **paired comparison**：同一个 packet 的 `rx_B - rx_A`，中位数从 +2.52 µs 变成 −1.20 µs，所以 B 相对 A 快了约 3.7 µs，而且每个负载档位都成立 |
| 6 | **分层后还有残余 confounding**：在 ≥100 msgs/100µs 的档位里，B 自己第 2 天还是比第 1 天慢 | "过去 100 µs 的消息数"只是队列深度的一个**不完美 proxy**，第 2 天的 burst 持续得更久。这正说明 paired / DiD 比单纯分层更可靠 |
| 7 | **Capacity knee**：1 Gb/s cross-connect 上每条消息在线路上约 122 B，大约每 100 µs 能发 102 条。延迟中位数从约 75 msgs/100µs（约 73% utilisation）开始离开基线，到 150 时约 89 µs | 按 rate 分 bin 画 p50/p99（y 轴取 log）。knee 定义为"从这里开始中位数一直比空闲基线高 2 µs 以上" |
| 8 | **Survivorship bias**：第 2 天 B 单线的 p99（110.7）**低于** arbitrated 的 p99（122.7） | B 丢掉的正好是在深队列里卡住的那些最慢的包；arbitration 从 A 把它们补了回来，它们又进了样本 |
| 9 | **float64 精度陷阱**：epoch ns 约 1.8e18，大于 2^53，在 float64 里的 ulp 是 256 ns。pandas 做 outer merge 时，有缺失值的 int64 列会**静默变成 float64**，于是出现大量假 "tie"，A−B 的差也被量化成 256 ns 的倍数 | 先减去当天零点，变成 day-relative ns（< 2^53，用 float 也精确）再 merge；或者用 nullable `Int64` |

**推荐的分析顺序**（面试时边说边做）：先做 integrity checks，再看 percentiles 和 log/CCDF 图，然后质疑绝对值（用 min-filter 查时钟），接着做 arbitration，再做天真的前后对比，发现 A（control）也变差了，于是怀疑两天流量不同，用 rate 分层，最后用 paired / DiD 给出结论。

---

## 2. Follow-up 参考答案

**Q1. 为什么要有 A/B feeds？为什么不用 TCP？**
- Market data 是一对多（几千个订阅者），要用 multicast。TCP 是点对点的，要给每个连接维护状态和重传，而且一个慢的 consumer 会拖累发送方。
- TCP 丢包后要重传，在重传完成前，后面的数据也交不到应用（head-of-line blocking），延迟会直接跳到 RTO 的量级（ms 级）。
- UDP 没有重传，所以靠**冗余**：两条物理路径独立，两条同时丢同一个包的概率约等于 p_A × p_B（前提是两者独立；上游丢包不独立，本题的 32 个 seq 就是例子）。
- 两条都丢时，用 retransmission server（通常走 TCP 或单播 request），或者用 snapshot/recovery feed 重建 book。

**Q2. 实时 arbitration 怎么写？两条都缺怎么办？等多久？**
```python
next_seq = 1; pending = {}          # seq -> packet，存提前到的包
def on_packet(pkt):                  # 两条线的包都进这里
    global next_seq
    if pkt.seq < next_seq or pkt.seq in pending:
        return                       # 另一条线已经送到过 -> 丢掉（这是 O(1) 的 dedupe）
    pending[pkt.seq] = pkt
    while next_seq in pending:       # 按顺序交给下游
        deliver(pending.pop(next_seq)); next_seq += 1
    if pending and gap_age() > GAP_TIMEOUT:
        request_retransmit(next_seq, min(pending) - 1)   # 或者标 book stale 并等 snapshot
```
- GAP_TIMEOUT 根据两条线到达时间差的分布来定（本题 |A−B| 的 p99 约 3–15 µs），比如设为 p99.9 再加一点余量。设太短会误报 gap，设太长会让下游 book 停住。
- 等待期间 book 标为 **stale**，下游策略不能用 stale book 下单。
- 复杂度：每个包 O(1) 均摊；pending 的大小受 gap 时长 × 消息率限制。

**Q3. 时钟同步？硬件 vs 软件时间戳？**
- 交易所和我们各自用 PTP（IEEE 1588），对 GPS 或 grandmaster 同步。典型精度：好的 PTP + 硬件时间戳可以到几十 ns 到 1 µs；NTP 在 ms 级。
- 两个不同时钟的 one-way latency = 真实延迟 + offset(t)。offset 无法单独测出来，除非做往返测量并假设路径对称。所以**跨时钟的绝对值只能当参考**，同一个接收时钟下的**差值**（A vs B，同一天内的变化）才可靠。
- 硬件时间戳：NIC 在包到达 PHY/MAC 时打戳，不受中断、调度、内核排队影响。软件时间戳（`SO_TIMESTAMP` 或用户态 `clock_gettime`）会混进中断合并、softirq、调度延迟，带 µs 级抖动。

**Q4. 为什么看 p99 不看 mean？什么时候 mean 有用？**
- 延迟分布右偏、重尾，mean 会被少数极端值拉动，也不对应任何真实的单个事件。交易里**最关键的时刻（行情剧烈、burst 时）恰恰在尾部**，而那正是赚钱和防风险的时候。
- mean 的用处：做容量规划和吞吐计算（Little's law：L = λW 用的是平均值），以及计算总成本和总时间。
- 注意：percentile **不能跨分片求平均**，要合并原始数据或可合并的 sketch（HDR histogram、t-digest）。

**Q5. Bootstrap 的假设？对 microburst 成立吗？**
- 普通 bootstrap 假设样本 iid。同一个 burst 里的包共享同一个队列，latency 强自相关，有效样本量远小于 n，所以 iid bootstrap 给出的 CI 会**过窄**。
- 解决：**block bootstrap**（本解按 1 秒分块重采样），或者先聚合成每秒的统计量再做推断。
- 参数检验（t-test）假设正态或 CLT，对 p99 这种分位数不适用；KS test 在 n ~ 10^5 时几乎总是显著（p≈0），只能说明"分布不同"，说明不了"哪边更好、好多少"。应该报告 effect size 和 CI。

**Q6. 还有哪些 confounder？**
时段（开盘那段更忙）、消息类型和包大小的组合（大包序列化更久）、交易所侧的变化（撮合引擎或网关的发布）、我方接收机的变化（CPU、NIC 驱动、内核）、PTP 状态（offset 不同），以及丢包导致的 survivorship bias。**同一个 packet 在 A、B 上的 paired 对比**能消掉其中绝大多数（交易所侧、流量组合、接收时钟都相同）。

**Q7. 用 queueing theory 解释 knee**
- M/M/1：平均等待时间 W = (1/μ) · ρ/(1−ρ)。ρ = 0.5 时等待约为 1 个服务时间，ρ = 0.8 时约 4 个，ρ = 0.95 时约 19 个。曲线是凸的，在 ρ → 1 时发散。
- 而且 burst 内的"瞬时"利用率远高于平均值。平均只有 1% 的链路也可能在 100 µs 里被打满。所以要按**微秒级窗口**看负载，不能看分钟平均。
- 结论：缩短 switch 的固定转发延迟（B 升级做的事）只能平移整条曲线；要压尾部，得**提高带宽**（10G/25G）或者降低 burst（分流到多个 multicast group 或多条 line）。

**Q8. 单线的 p99 能比 arbitrated 更好吗？**
能。arbitrated 的每个包的延迟 ≤ 任一条线上同一个包的延迟，但两者的**样本集合不同**：单线会丢包，而丢的正好是深队列里最慢的那些（tail drop），它们在单线统计里消失了，在 arbitrated 里又被 A 补回来。这就是 **survivorship bias**。比较时应该在同一组 seq 上比，或者把丢包当成延迟 = ∞ 来处理。

**Q9. 200 天 × 5000 万包/天，怎么存、怎么算？**
- 存储：Parquet，按 `date / feed / line` 分区，列式存储加压缩（时间戳做 delta 编码后很小）。只读需要的列。
- 计算：按天、按 line 并行（embarrassingly parallel）。每天只算可合并的摘要：HDR histogram（固定的对数 bucket，可以直接相加）、按 rate bucket 的计数，以及丢包的 seq 列表。跨天合并摘要，不重复扫原始数据。
- 工具：pandas 处理单天没问题（5000 万行 × 几列，约 1–2 GB）；规模再大可以用 polars / DuckDB / Spark，或者用 pyarrow dataset 流式读取，按 row group 迭代。

---

## 3. 常见错误 / 扣分点
1. 只报 mean，或者 histogram 用线性坐标，把尾部压成看不见的一条线。
2. 把 `rx - exch` 当成真实的物理延迟直接下结论（"我们离交易所 24 µs"），没意识到里面有时钟 offset 和漂移。
3. 忘了 dedupe，或者用 `seq.max() - len` 算丢包，没处理 duplicate（会把丢包数算少）。
4. outer merge 后的 int64 → float64 精度丢失（epoch ns），出现大量假 tie。
5. 看到天真对比里 p99 变差，就说"升级让尾部变差了"，没用 A 当 control，也没检查两天的负载是否可比。
6. 用 KS 的 p 值当 effect size；bootstrap 用 iid 重采样却不提自相关。
7. 分层以后就停下，没意识到 proxy 不完美、高负载档里还有残余 confounding；没想到 paired 对比这个最强的设计。
8. 没提"两条线都丢"的 seq 需要 recovery。这是 feed handler 的正确性问题，比延迟更要紧。

---

## 4. Speak-aloud scripts (English)

**On distrusting the absolute numbers:**
> "Before I compare anything, I want to know if `rx minus exch` is a real one-way latency.
> These are two different clocks, so it's true latency plus an unknown offset. If I take the
> per-minute minimum, which strips out queueing, I see the same +1 µs per hour slope on both
> lines and both days. A network path doesn't speed up and slow down identically on two
> independent lines, so that's clock drift. I'll trust *differences* measured on our clock,
> A versus B, and treat the absolute level as approximate."

**On the upgrade verdict:**
> "Naively, B's median improved by 3.4 µs but its p99 got 40 µs worse. Line A didn't change
> at all, and its p99 got even worse, so something about day 2 itself is different. Day 2 had
> about three times as many messages in microbursts. The cleanest comparison is paired:
> the same packet travels on both lines, so B minus A per packet cancels the traffic, the
> exchange and the clock. That goes from +2.5 µs to −1.2 µs, in every load bucket. So yes,
> the upgrade made B about 3.7 µs faster. The tail problem is capacity, not the switch."

**Recommendation to the network team:**
> "Both lines start queueing at around 75 messages per 100 µs, which is roughly three
> quarters of a 1-gig link. The tail is a bandwidth problem, so I'd prioritise upgrading the
> cross-connect over further switch tuning. Also, B tail-drops packets during bursts. Deeper
> buffers or more bandwidth would fix both."

---

## 5. 复杂度
- Integrity checks：dedupe 和 groupby 都是 O(n)（hash），乱序检测用 `cummax` 一遍扫完，O(n)。
- 丢包：集合差 O(n)；数 run 先排序 O(k log k)（k = 丢包数）。
- Rate（过去 100 µs 的消息数）：exch_ns 已排序，`searchsorted` O(n log n)；双指针可以做到 O(n)。
- Arbitration merge：hash join O(n_A + n_B)。实时版本每包 O(1) 均摊。
- Block bootstrap：每次重采样 O(n)，总共 O(B·n)，本解 B = 200，约 3 秒。
- 内存：两天两条线约 150 万行 × 6 列，约 100 MB，单机没压力。
