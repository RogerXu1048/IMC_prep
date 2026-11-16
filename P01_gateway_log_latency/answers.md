# P01 参考答案 — Order Gateway Logs

> 先自己做完再看。运行 `python3 solution.py` 可以看到全部输出和图（`plots/p01_*.png`）。

## 1. 预设的 findings 以及怎么发现

| # | Finding | 怎么发现 |
|---|---|---|
| 1 | **gw-3 周期性卡顿**：每 10 秒（整 10 秒的 wall-clock 边界，如 14:31:20.000）开始约 120 ms 的窗口内，订单多出 1–4 ms 的 internal latency。只影响 gw-3 约 1.1% 的订单，但 gw-3 的 p99 从 ~17 µs 变成 ~1.3 ms，mean 从 ~8 µs 变成 ~35 µs，而 **median 完全正常**。 | Part C 的表格里 gw-3 只有 p99/p99.9/max 异常 → Part D 画 latency vs time（log y）看到慢单是一条条“竖线” → 计算慢单所在秒的间隔，众数=10 s → 把时间按 10 s 折叠 (`t % 10s`)，所有慢单都落在 phase 0–120 ms。结论：某个周期性任务（log flush、stats snapshot、cron、GC、THP compaction 等）和 gateway 线程抢 CPU。排除这个窗口后 gw-3 的 p99 = 17 µs，和其他 gateway 一样。 |
| 2 | **Open 时的 venue RTT 变差**：14:30:00–14:30:30 订单速率约 5×，所有 venue 的 RTT median ×1.5、尾部更重（ARCX p99 ~109 µs → ~295 µs）。 | 按 venue × (open / 非 open) 分组；或者画 5 秒粒度的 p99 RTT 时间序列，看到 14:30 的尖峰。这是 **负载相关** 的，不是 bug，但交易员需要知道“开盘时的 RTT 不是平时的 RTT”。 |
| 3 | **Venue 基线差异**：RTT median XNAS ≈ 34 µs、ARCX ≈ 54 µs、BATS ≈ 29 µs。 | Part C by venue 表格。 |
| 4 | **ARCX 无响应事件**：14:36:41–14:37:01 约 20 秒，ARCX 的订单约 58% 没有任何 ACK/REJ（共 ~260 单），其他 venue 正常。全天另有 ~25 个零散的 no-response。 | 把 NO_RESPONSE 订单按时间排序，相邻间隔 > 5 s 就切一个 cluster（简单的 gap-based clustering）→ 只有一个大 cluster，几乎全是 ARCX。再看窗口内各 venue 的丢失比例确认是 venue 问题而不是我们某个 gateway 的问题（4 个 gateway 都受影响）。 |
| 5 | **RATE_LIMIT rejects 只出现在 gw-2、只在 14:30:00–14:30:29**（~320 个）。 | `pd.crosstab(reason, gw)` + RATE_LIMIT 的时间范围。说明 gw-2 在开盘 burst 时超过了交易所的 message rate 限制 → 需要在 gateway 侧做 throttling / 把流量分摊到其他 session。 |
| 6 | **内部错误**：46 个 ERROR + traceback 的订单有 IN 没 OUT（订单根本没发出去）；另外几单因为日志行被截断而缺 IN 或 OUT。 | status = INTERNAL_ERROR / NOT_SENT / INCOMPLETE_LOG。 |

数据质量方面（Part A）：~540 行完全重复、~1300 行文件顺序与时间顺序不一致（多线程写日志）、258 行 traceback 续行、32 行 malformed（日志轮转截断）、gw-4 用 9 位纳秒小数而其他 gateway 用 6 位微秒。

### 关键分析路径（面试时按这个顺序讲）
1. 先 `head` 看格式 → 写 prefix regex + body 用 split。
2. 数据质量报告（数字！）→ 说明处理策略：“我按 clid 配对、不依赖文件顺序；完全重复行去重；同一 clid+event 不同时间戳算 conflict 单独报告”。
3. 先给一个整体数字回答问题（median internal ≈ 8 µs NewOrder / 5 µs Cancel），然后 **立刻切片**：by gateway、by type、by venue、by time。
4. 发现 gw-3 尾部异常 → 时间序列 → 周期 → fold → root cause。
5. 无响应 / reject 的分布，提出生产上的处理。

## 2. Follow-up 参考答案

**Q1. Average vs percentiles。** Latency 分布是右偏、重尾的（lognormal-ish + 偶发的 ms 级 outlier）。Mean 对 outlier 极其敏感：gw-3 的 1% 慢单就把 mean 从 8 µs 拉到 35 µs，而 median 没变，所以 mean 既不代表“典型订单”也不能告诉你尾部有多差。交易里真正伤人的是尾部：抢单（race）的时候，慢的那 1% 往往正是行情剧烈、最有价值的时刻。所以报告 p50（典型）、p99/p99.9（尾部）、max（最坏情况）。Mean 仍然有用的地方：容量规划（总 CPU 时间 = mean × count）、Little's law（队列长度 = 到达率 × 平均停留时间）、以及作为 sanity check（mean ≫ median 本身就是“有重尾”的信号）。

**Q2. 能不能平均 p99？** 不能。Percentile 不可加：40 个 shard 各自的 p99 的平均值既不是全局 p99 的上界也不是下界（solution 里：真实全局 p99 = 17.9 µs，而 4 个 gateway p99 的平均 = 328 µs，因为 gw-3 一个把平均拉飞；反过来如果流量不均匀也可能低估）。正确做法：每个 shard 输出**可合并的 sketch**——HDRHistogram（固定相对精度的对数桶，合并 = 桶计数相加，误差有界）、t-digest、DDSketch、或者最简单的固定桶 histogram；在中心把 sketch merge 后再查 quantile。另外要注意：全局 p99 本身也会**掩盖**单个 gateway 的问题（这里 gw-3 的慢单只占全部订单的 0.28%，全局 p99 根本看不出来）→ 监控必须按 gateway 切片。

**Q3. 时钟。** IN 和 OUT 在同一台机器、同一个时钟源上打的时间戳 → 差值可信（只要时钟是单调的、没有被 NTP step 调整；严格来说应该用 `CLOCK_MONOTONIC` 或 TSC，而不是 wall clock）。OUT→ACK 也是同一台机器（ACK 是我们收到回报的时间），所以 RTT 也可信，它包含网络往返 + 交易所处理。真正麻烦的是 **跨机器** 的差值（例如 strategy 机器的发送时间 vs gateway 的 IN 时间，或者交易所时间戳 vs 我们的接收时间）：NTP 精度通常是 0.1–几 ms，完全不能用来测 µs 级延迟；PTP（IEEE 1588）+ 硬件时间戳可以到几十~几百 ns。跨机器的 one-way latency 要么用 PTP 同步的硬件时间戳，要么只测 round trip（同一时钟），要么用网络 tap/交换机的硬件时间戳在同一个时钟域里测。

**Q4. 时间戳精度。** 1 µs 分辨率测 5–8 µs 的延迟，量化误差是 ±1 µs，相对误差 10–20%；在直方图里能看到离散的“条纹”（solution 的散点图里 gw-1..3 的值都在整数 µs 上）。对 median/p99 的比较影响不大，但对亚微秒级的优化（例如“我们省了 300 ns”）就无法测量了 → 需要 ns 级时间戳（gw-4 的新版本）、或者用 TSC/硬件时间戳。还要注意打时间戳本身的开销（`clock_gettime` ~20 ns，格式化成字符串要几百 ns 甚至 µs —— 所以热路径上应该记录原始整数，异步格式化）。

**Q5. Coordinated omission。** 当测试/监控系统在被测系统卡住时**也停下来不发请求**，卡顿期间本应被测到的大量“慢请求”就消失了，测出来的尾部严重低估（Gil Tene 的经典演讲 “How NOT to Measure Latency”）。这里：gw-3 卡顿的 120 ms 里如果上游 strategy 因为 back-pressure 少发了订单，我们看到的慢单数就少于“真实受影响的机会数”。修正方法：按固定计划的发送时间（intended start time）而不是实际发送时间计算延迟；HDRHistogram 提供 `recordValueWithExpectedInterval` 来补偿；或者在报告时同时看“受影响的时间占比”（这里 120 ms / 10 s = 1.2% 的时间不可用），而不只是“受影响的订单占比”。

**Q6. 异步 logger。** 如果时间戳是在 logger 线程 **写日志的时候** 取的，而不是事件发生时，那么 logger 队列的排队延迟会混进你的测量里，甚至造成乱序（这里就有 ~1300 行文件顺序≠时间顺序）。正确做法：在热路径上事件发生的那一刻取时间戳（整数 ns），作为字段传给 logger；logger 只负责格式化和 I/O。分析时永远用事件里的时间戳、按 ID 配对，不能依赖文件顺序或者“上一行”。

**Q7. Regex vs split。** `str.split()` 是一次 C 层的线性扫描，几乎没有回溯，通常比复杂的 regex 快 2–5×；regex 更严格、更容易做验证。实用的折中（solution 的做法）：一个**锚定**的 prefix regex（`^` 开头、无回溯风险）解析时间戳/gateway/level，body 用 `split()` + `dict(t.split('=',1))`；预编译 regex；对时间戳做缓存（15 分钟只有 900 个不同的秒，`lru_cache` 后 strptime 只调用 900 次）。我的 parser ~400k lines/s。看时间花在哪：`cProfile` / `py-spy`，通常是 regex match、dict 构造、`int()` 转换和 DataFrame 构造。更快：`pyarrow.csv` 或 `pd.read_csv(sep=' ')` 先粗切再向量化（`str.extract`），或者 C/Rust 写的 parser。

**Q8. 内存复杂度。** Part A 的 generator 是 O(1)；但我把所有事件放进 DataFrame 是 O(N)。真正流式的做法（Part F）：只保存 **in-flight** 订单的状态（`clid → t_in`，看到 OUT 就配对并删除），内存 O(同时在途的订单数) + O(直方图桶数)。要注意：(a) 行乱序 → OUT 可能先于 IN 出现，所以两边都要有 pending dict；(b) 重复行 → 已完成的 clid 会被重新打开，需要一个有界的“最近完成”集合（solution 用 `OrderedDict` 做 FIFO 淘汰）；(c) 永远不会配对的订单（无响应）需要 TTL 清理，否则内存泄漏。

**Q9. 告警。** 按 gateway / venue 分别维护滑动窗口（例如 10 s）的 p99（用 histogram），与该时段的基线（同一时间段的历史值，开盘本来就慢）比较；连续 N 个窗口超阈值才告警（降噪）；“无响应订单数”和“reject rate 按 reason”是独立的告警，因为它们代表**状态不确定**的风险（比 latency 更严重）。告警要发给对的人：gw-3 周期卡顿 → gateway/infra 团队；ARCX 无响应 → 交易台 + 交易所联系人；RATE_LIMIT → strategy/gateway throttling 负责人。

## 3. Part E：无响应订单在生产上怎么办
- 这个订单的状态是 **unknown**，不是 “failed”：它可能已经在交易所成交了。**不能**直接重发（可能双倍成交、风险敞口翻倍）。
- 做法：per-order timeout（例如 RTT p99.9 的几倍）→ 标记 `PENDING_UNKNOWN` → 发 order status request / 依靠交易所的 drop copy 对账 → 风控按“可能已成交”计算敞口 → 如果 session 断了，依赖 cancel-on-disconnect；venue 级别出现集中性无响应时，自动暂停往该 venue 发单并通知交易台。

## 4. Part F：200 GB/day、40 台 gateway
- 不要把日志集中到一台机器上用 pandas：每个 gateway 本地（或按 gateway 分区的 Kafka topic）流式解析，输出 (a) 每秒/每分钟的 HDRHistogram，(b) 异常事件（无响应、reject、ERROR）。中心节点合并 histogram，存时序库（按时间分区）。
- 更好：热路径直接输出二进制的结构化事件（不要先写文本再 parse 回来），日志只给人看。
- 分区键：gateway（天然独立）；时间（按小时/分钟的文件或 parquet 分区），便于并行和重算。
- 批处理复算（例如修正 bug 后重跑）用 parquet + 按日期/gateway 分区，多进程并行。

## 5. 常见错误 / 扣分点
- 依赖文件顺序（“上一行的 IN 配这一行的 OUT”）→ 被乱序行坑。
- 没去重 → 重复的 OUT 可能产生负延迟或重复计数。
- 用 `pd.to_datetime` 直接解析混合 6 位/9 位小数而不检查 → 不同 pandas 版本行为不一，最好自己统一到整数 ns。
- 只报 mean；或者只报全局数字，不切片，看不到 gw-3。
- 线性坐标的直方图（所有东西挤在左边一根柱子里）。
- 把 “no response” 当成 “rejected” 或者直接丢掉。
- 平均各 gateway 的 p99。
- 时间都是 UTC 却在讨论“开盘”时没换算（14:30 UTC = 9:30 ET，11 月是 EST）。

## 6. English speak-aloud scripts

**Opening (before coding):**
> "Before I compute anything I want to see the raw format and the data quality. I'll write a streaming parser — an anchored regex for the common prefix and a split for the key-value body — and I'll pair events by client order id rather than by file order, because the log is multi-threaded and lines can be out of order or duplicated. I'll report how many lines I dropped and why, so we know how much to trust the numbers."

**Answering 'what is the average':**
> "The median internal latency is about 8 microseconds for new orders and 5 for cancels, and it's the same on all four gateways. But the mean is misleading here: on gw-3 the mean is 35 microseconds while the median is 7, because about one percent of its orders take one to four milliseconds. So I'd report p50, p99 and p99.9 per gateway — the tail is where the money is lost."

**Root cause:**
> "The slow gw-3 orders aren't random: the gap between slow clusters is exactly ten seconds, and when I fold the timestamps modulo ten seconds, every slow order falls in the first 120 milliseconds of the cycle. That points to a periodic job on that host — a log flush, a stats snapshot or a cron task — competing for the gateway's core. Excluding that window, gw-3 looks identical to the others. I'd check what's scheduled every ten seconds on that box and pin the gateway thread to an isolated core."

## 7. 复杂度
- 解析：O(L) 时间（L = 行数），generator O(1) 内存；DataFrame 化后 O(N)。
- pivot / groupby：O(N log N)（排序/哈希分组），quantile 需要每组排序 O(n log n)（或 selection O(n)）。
- 流式配对：每个事件 O(1) 平均（hash map），内存 O(in-flight)。
- Log-bucket histogram：add O(1)，merge O(B)，quantile O(B log B)（排序桶，可以维护有序数组变成 O(B)），内存 O(B)，B ≈ log(max/min)/log(1+ε) —— 1 µs 到 10 s、1% 精度只要 ~1600 个桶。
