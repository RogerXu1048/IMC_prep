# P08 参考答案 —— 行情流上的算法设计

> 先自己做完再看。数字来自 `solution.py`（默认 seed）。测试：`python3 test_solution.py`（12 个 brute-force 对拍测试）。

## 1. 预设的 findings 以及怎么发现

| Part | 埋的点 | 怎么发现 | 参考结果 |
|---|---|---|---|
| A | 每个 port 文件按**到达顺序**写，event time 有 ≤2ms 的乱序 | 直接 `heapq.merge` 后数“时间倒退”次数 | 36,935 次倒退 → 假设不成立 |
| A | 每个 port 有 5 条 “very late” 消息（晚到 16–74ms） | watermark reorder（D=2ms）的 side output | 正好 20 条（每 port 5 条）；buffer 峰值 160 条/port（出现在 microburst 时） |
| B | 12 次 sub-ms microburst（平均只有 1.28 msgs/ms） | 双指针求任意 1ms 窗口最大消息数；>50 的窗口合并 | 最大 594 msgs/ms；合并后正好 12 个 burst |
| C | S2、S3 各有一次突发违规；S1 的突发**正好 50 条**（边界，不违规） | 每个 session 双指针 | S1 peak=50 → 0 违规（off-by-one 测试点）；S2 20 条违规；S3 59 条违规 |
| C | token bucket(50, 50/s) ≠ “任意 1s ≤ 50” | 用 limiter 回放后再数 rolling 1s 最大值 | sliding log：最坏 50 → OK；TB(50,50/s)：最坏 75 → **仍违规**；TB(25,25/s)：47 → OK 但多拒了单 |
| D | t=180–210s 延迟整体 ×4（拥塞） | rolling p50/p99 画图；按秒汇总，找持续升高 | p50 25µs → ~100µs；p99 91µs → 300–800µs；孤立的 p99 尖刺是尾部噪声 |
| E | 一个平时排第 9 的 symbol（YY）最后 60s 突然很热 | 滑窗 top-K vs 全局 top-K | 最后 60s 排第 3；Misra–Gries(k=20) 全局 top-5 正确，最大少计 10,059 ≤ N/(k+1)=18,312 |

## 2. 各 Part 的思路与复杂度

### Part A
- **(i) 已排序的 k 路归并**：`heapq.merge(*streams, key=...)`，堆里永远只有 k 个元素：时间 O(N log k)，内存 O(k)。brute force：全读进来 `sorted` → O(N log N) 时间、O(N) 内存，而且不是流式的。
- **(ii) 有界乱序**：每个 port 一个 min-heap reorder buffer；`max_seen` 是见过的最大 ts，**watermark W = max_seen − D**。所有 ts ≤ W 的元素可以安全输出（因为按假设之后不会再来比 W 更早的消息）。到达时 ts < W 的消息是 **late**：输出它会破坏顺序 → 放进 side output（Flink 的 `sideOutputLateData`）。然后再对 4 个已排序的输出做 `heapq.merge`。
  - 时间 O(N log b)，内存 O(b)，b ≈ rate × D（平时 ≈ 1 条，microburst 时 160 条）。
  - 为什么“先 per-port reorder 再 merge”：乱序保证是 per-port 的；跨 port 没有统一的到达顺序。
  - Late 消息怎么办：计数 + 告警（说明 D 设小了或网络有问题）；研究场景可以写到“修正流”里离线补；实时场景通常直接丢弃或单独处理。
- 用 `seq` 打破 heap 中 ts 相同时的比较（不要让 Python 去比较 dict）。

### Part B
- brute force：对每个消息往回数 → O(N²)；或者按 1ms 日历桶计数 → O(N)，但**会漏掉跨桶的 burst**（一个 burst 一半落在 x.9995 一半在 x.0005，每个桶只看到一半）。
- 双指针：right 每前进一步，left 前进到 `ts[right] − ts[left] < w`；窗口内计数 = right − left + 1。O(N) 时间、O(1) 额外内存（对已排序数组）。
- 合并：计数 > 阈值时窗口区间 `[ts[left], ts[right]]`，和上一个区间重叠就延长，记录 peak。

### Part C
- 违规检测：per-session 双指针，O(N)。**边界**：`(t − 1s, t]`，所以 `t − ts[left] >= 1s` 时 left 才前移；S1 正好 50 条不违规——这是故意埋的 off-by-one 测试。
- **SlidingWindowLimiter**（sliding log）：deque 存最近被接受的发送时间；`allow(t)`：弹出 ≥1s 之前的，长度 < 50 才接受。O(1) 摊还，内存 O(limit)。它是**精确**满足规则的、并且是贪心最优的（能发就发）。
- **Token bucket**：O(1) 时间/内存，但它限制的是长期速率；任意 1s 窗口内最多可以发 capacity + rate×1s 条（满桶 + 窗口内补充）。数据里 TB(50,50/s) 最坏 75 条/秒 → 违规。安全配置需要 capacity + rate × window ≤ limit（例如 25 + 25），代价是拒绝更多正常单（91 vs 57）。
- 还可以提：sliding-window counter（两个固定窗口加权近似）、GCRA；生产中 limiter 留一点 headroom（比如 45/s）以防时钟差异和交易所计数口径差异。

### Part D
- brute force：每条消息对窗口排序 O(W log W) → 实测 ~26k msgs/s。
- **Fenwick tree（BIT）over 整数 µs 桶**：值域 B=32,768（更大的值 clamp 到最后一个桶，对 p50/p99 无影响，但要说出来）。add/remove O(log B)；k-th smallest 用 binary lifting O(log B)。实测 ~310k msgs/s（同时算 p50+p99）。内存 O(B + W)。
- **双堆 + lazy deletion**（sliding window median）：small（max-heap）放下半部，large（min-heap）放上半部；删除时只在 `dead` 计数里记一笔，等它浮到堆顶再真正弹出；用“活元素个数”而不是 `len(heap)` 做平衡。O(log W) 摊还，median O(1)，**不依赖值域**。实测 ~1.36M msgs/s（heapq 是 C 实现）。只能求中位数（或固定分位，要改比例）。
- 三者在数据上逐条一致（solution 里有 assert），tests 里还用随机小数据对拍。
- **看到了什么**：t=180–210s 持续性的 p50/p99 抬升 ×4 → 一次 30s 的拥塞事件；而 p99 的孤立尖刺是因为 1000 个样本的 p99 只由 ~10 个尾部样本决定，天然很抖——告警要看**持续性**或用更大的窗口。

### Part E
- 精确滑窗：deque[(ts, sym)] + Counter；每条消息 O(1) 摊还（每个事件进出各一次）；查询 `heapq.nlargest(k, counter.items())` O(S log k)。内存 O(窗口内消息数)。
- **Misra–Gries**（k 个计数器）：新元素若在表中 +1；表未满就插入；否则所有计数 −1、删 0。保证：估计值 ≤ 真实值，且 ≥ 真实值 − N/(k+1)；频率 > N/(k+1) 的元素一定在表里。内存 O(k)。只会**少计**，因为每次“全体减一”都只减不加。
- **Count-Min sketch**：d 行 × w 列计数器，每行一个 hash；更新时 d 个格子都 +1，查询取 d 个格子的**最小值**。只会**多计**（hash 冲突只会加）；误差 ≤ εN（w = e/ε）以概率 1−δ（d = ln(1/δ)）。优点：可以查询任意 key、可以**合并**（两个 sketch 按格子相加）、支持滑窗变体；缺点：本身不存 key，求 top-K 需要再配一个小 heap。

## 3. Follow-up 参考答案
1. **k=2** 就是普通双指针 merge，O(N)；**k=10,000**：heap 仍是 O(N log k)；也可以用 tournament/loser tree（比较次数更少、cache 更好）；或者分层归并。如果 key 是有界整数（如 ns 时间戳落在一天内）也可以 bucket/radix。
2. **D 的选择**：从历史数据测“迟到量”的分布（每条消息相对 max_seen 晚了多少），取能覆盖 99.99% 的值再加 margin。D 越大：late 越少，但**输出延迟 = D**、buffer 内存 = rate × D 越大。实时交易路径上 D 必须很小；研究/离线可以很大甚至全排序。
3. **静默的 port**：如果按“所有 port 的 min watermark”做全局合并，一个没消息的 port 会让 watermark 不前进 → 卡住。解决：idle timeout（Flink 的 `withIdleness`），或用 heartbeat/心跳包推进该 port 的 watermark；用 processing time 兜底。
4. 半开区间方向不影响“最大值”（对称），但会影响边界上的计数和 throttle 的 off-by-one。日历桶计数会漏掉跨桶的 burst，最坏情况下低估接近 2 倍。
5. token bucket 简单、O(1)、内存常数、天然支持“平均速率 + 允许小突发”；很多交易所的限流其实就是 token bucket 或固定窗口计数。关键是**读交易所的规则原文**，把我们这边的 limiter 做得比交易所更严格。
6. 用 **HDR histogram**（对数分桶 + 每个数量级内线性子桶，相对误差有界，比如 0.1%），或 log-bucketing，或 t-digest / KLL sketch；HDR 和固定桶直方图都可以**直接相加合并**。
7. **p99 不能合并**（两个 shard 的 p99 求平均不是全局 p99）。可以合并的是：直方图/HDR 计数、t-digest、KLL 等 mergeable sketch；或者原始数据。
8. 见上：MG 只做“全体减一”→ 只少计，误差 ≤ N/(k+1)；CM 只做加法、hash 冲突只会叠加 → 只多计。
9. 热路径：rate limiter（必须，且要 O(1)、无分配）、可能的 microburst 计数器。离线/旁路：k-way merge 做研究数据、rolling 分位数做监控、heavy hitters 做监控面板。
10. 对拍测试：随机生成小输入，和 O(N²)/排序的 brute force 比较；特别覆盖：空输入、全部相同时间戳、窗口边界恰好相等、重复值（双堆 lazy deletion 最容易错）、非常晚到的消息、限流恰好等于 limit。

## 4. 常见错误 / 扣分点
- 直接 `sorted()` 全部数据——没有回答“流式”的要求；或 `list(generator)` 把所有东西读进内存。
- 堆里放 `(ts, dict)`，ts 相同时 Python 比较 dict 报 `TypeError`。
- 用日历桶近似滑动窗口、或窗口边界 off-by-one（S1 那个正好 50 的 burst 会把你抓出来）。
- 说 “token bucket(50, 50/s) 就保证每秒不超过 50”。
- 双堆删除时用 `len(heap)` 做平衡（堆里有已删除的 dead 元素）。
- 用一个 1000 样本的 p99 的单点尖刺去告警。
- 不说复杂度、不写对拍测试。

## 5. Speak-aloud scripts (English)

**Clarify + brute force first:**
> "Let me restate the guarantee: within a port, delivery is out of event-time order by at most 2 ms. The brute-force answer is to read everything and sort, which is O(N log N) time and O(N) memory and isn't streaming. Since the disorder is bounded, I only need to hold back the last 2 ms of data: I keep a min-heap per port and a watermark equal to the max timestamp seen minus 2 ms. Anything at or below the watermark is safe to emit. Anything that arrives below the watermark is late, and I route it to a side output and count it rather than silently reordering history."

**Token bucket vs sliding window:**
> "A token bucket with capacity 50 and refill 50 per second limits the long-run rate, but in any one-second window it can let through the full bucket plus one second of refill — up to about 100. Replaying the S3 burst shows 75 orders in a single second. If the exchange rule is a rolling window, I'd use a sliding log, which is exact and still O(1) amortized, or size the bucket so that capacity plus rate times the window stays under the limit."

**Rolling percentiles:**
> "Latencies are integers in microseconds with a bounded range, so I can keep counts in a Fenwick tree: each update is O(log B) and the k-th smallest is a binary-lifting walk down the tree, also O(log B). I verified it against brute-force sorting on every message. One caution: p99 over a thousand samples is determined by about ten samples, so I'd alert on sustained elevation rather than single spikes — which is exactly what separates the real 30-second congestion episode from tail noise in this data."
