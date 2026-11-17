# P03 参考答案 —— L2 订单簿重建 + 微观结构统计

> 先自己做完再看。数字来自 `solution.py` 在默认 seed 下的输出（重新生成数据数字不变）。

## 1. 预设的 findings 以及怎么发现

| # | 埋的点 | 怎么发现 | 参考数字 |
|---|---|---|---|
| 1 | 1 行坏掉的 JSON（截断的重复行） | `try/except json.JSONDecodeError` 计数，不让程序崩 | 第 80400 行 |
| 2 | heartbeat 没有 `seq` | inventory message types；用 `m.get("seq")` | 1,799 条 |
| 3 | ~0.1% duplicate seq | 用 `set` 记录见过的 seq | 215 条 |
| 4 | ~0.2% 乱序（晚到 1–3 个位置） | arrival order 下 `seq < max_seen` | 857 条到达时 seq 比最大值小 |
| 5 | 连续 3 个 seq 丢失 | `set(range(min,max+1)) - seen` | 101534–101536（一笔吃光 best level 的 trade + 它的 update + 下一条） |
| 6 | 价格浮点噪声 | `px != round(px, 2)`；`int(px*100) != round(px*100)` | 5,408 个有噪声，其中 4,351 个用 `int()` 会**错一个 tick** |
| 7 | event-weighted vs time-weighted spread 不同 | busy regime 时 trade 多、做市商补单慢 → spread 宽，而且 busy 时消息多 → 按事件平均会高估 | event 1.63 ticks vs time 1.32 ticks；1-tick spread 占 60% 的事件但 77% 的时间 |
| 8 | imbalance 预测下一次 mid 变化 | decile 分箱（只用前半段算分位点），后半段验证 | P(up) 从 0.23 单调升到 0.79；OOS 方向准确率 66% |
| 9 | 消息量和波动率相关 | per-minute msgs vs realized vol | corr ≈ 0.90 |

**正确的 rebuild 结果**：live rows 和 truth 100% 一致；snapshot 对账 42 次 0 次不一致；stale 区间 = seq 101537..105000（直到 seq 105001 的 snapshot）。
**Naive（按到达顺序直接 apply）**：1.86% 的 seq BBO 是错的，并且出现 **2,461 行 crossed book（bid ≥ ask）**——因为丢掉的那条 update 是“best ask 被吃光、qty=0”，naive book 里留下了一个 phantom level。

分析路径（面试时建议按这个顺序讲）：
1. 先审计数据（A）→ 发现 dup/乱序/gap/坏行/价格噪声 → 再决定 policy。
2. Rebuild 时把 policy 写成状态机：`WAIT_SNAPSHOT → LIVE → (hole 持续超过 reorder window) → STALE → (snapshot 或 hole 被补上) → LIVE`。
3. 用 truth 做回归测试；用 snapshot 做在线对账（free checksum）。
4. 再做统计：先问“这个统计应该按什么加权”。

## 2. Follow-up 参考答案

**Q1 为什么用 integer tick？为什么不能 `int(px*100)`？**
浮点数不是精确的十进制：`100.13` 可能变成 `100.13000000000001` 或 `100.12999999999`。作为 dict key 会出现“两个看起来一样的价格”变成两个 level；比较 `bid >= ask` 也可能出错。`int()` 是截断，`100.12999999999*100 = 10012.999…` → `10012`，错一个 tick。用 `round(px*100)`，或者最好在 parse 时就用 `Decimal`/定点整数。生产系统里 feed 本身通常就是定点整数（price × 10^4 之类）。

**Q2 为什么不能按 timestamp 排序再 apply？**
(a) 很多消息 timestamp 完全相同（同一个 matching event 产生 trade + update），排序不稳定就会打乱因果；(b) timestamp 可能来自不同时钟/不同阶段，seq 才是交易所定义的 mutation order；(c) 实时系统里你不能“等所有数据到齐再排序”。正确做法：按 seq，必要时用小的 reorder buffer。

**Q3 收到 1003 但 1002 没到？**
先 buffer 1003（不能 apply，因为 1002 可能删掉了某个 level）。等待一个**有界**的窗口（按消息数，比如 20 条；或按时间，比如几百 µs~几 ms，看 feed 的乱序特征），期间 book 不发布或标记“pending”。超时则宣布 gap：book 标 stale，向下游广播，发 retransmission request（如果交易所支持 gap-fill/replay），或者等下一个 snapshot。snapshot 到了：丢弃 ≤ S 的 buffered 消息，用 snapshot 替换 book，再 replay > S 的连续消息。如果 1002 在宣布 gap 之后才到 —— 依然可以补上（我的实现里 `_drain` 会把 stale 清掉）。

**Q4 absolute-size update 的 duplicate 有害吗？**
单独看是幂等的（再设一次 qty=500 没变化），**但如果中间有别的 update 改过同一个 level**，晚到的 duplicate 会把它改回旧值。所以仍然要按 seq 去重。增量型（delta：+200/−100）的 feed 更危险，duplicate 直接导致数量错误。

**Q5 event-weighted vs time-weighted？**
问题是“一个随机时刻我看到的 spread 是多少”（比如你要在某个时间点下单的成本）→ time-weighted。问题是“每次事件发生时的 spread”（比如 trade 发生时的有效 spread、每次报价更新的状态）→ event-weighted 或 trade-weighted。本题中 busy 时段消息多且 spread 宽，所以 event-weighted 偏高（1.63 vs 1.32）。这就是 inspection paradox / 采样偏差。

**Q6 L2 vs L3？**
L2 只给每个价位的总量；L3（MBO, market-by-order）给每个订单的 id、数量、先后顺序。L2 看不到：**queue position**（我的单前面还有多少量）、订单数（1 个 1000 股还是 10 个 100 股）、单个订单的 cancel/modify 行为、谁先成交。做市商关心 queue position，因为它决定 fill probability 和 adverse selection。

**Q7 snapshot 晚到 / snapshot 走另一个 channel？**
snapshot 有自己的 seq 边界 S：拿到后只 replay seq > S 的增量，所以增量流要一直 buffer（有界）。如果 snapshot 在另一个 channel（很多交易所是独立的 snapshot/recovery multicast 或 TCP replay），就需要在 snapshot 到达前把增量缓存下来，snapshot 到达后丢掉 ≤ S 的部分再 replay；如果 buffer 里最小的 seq 已经 > S+1（中间又缺了），要等下一个 snapshot。snapshot 太老（S 小于已 apply 的 seq）就忽略或只用来对账。

**Q8 dict 不排序，best bid 怎么找？**
`max(dict)` 是 O(L)（本题 50 层时约 330ns/次）。改进：缓存 best，只有 best level 被删除时才 rescan（摊还很便宜，因为删除 best 的频率远小于消息频率）；或用 heap + lazy deletion（best O(1)，插入 O(log L)）；或有序结构（balanced tree / skip list：插入删除 O(log L)，top-K O(K)）；或价格有界时用 **tick-indexed array + 指针**（O(1) 更新，best 移动时线性扫描到下一个非空 tick，通常只扫几格，cache 友好 —— 交易所/HFT 常用）。Python 标准库没有 sorted map；`OrderedDict` 是插入顺序，不是价格顺序。

**Q9 怎么测试？**
- Unit：BookSide.set 的边界（删除 best、删除不存在的 level、qty=0、空 book）。
- Scenario：手写小 feed：dup、乱序、gap 后 snapshot、gap 后迟到补洞、snapshot 早于 buffer 中的消息。
- Regression：把 truth/golden BBO 存下来，每次改动后比较（本题 truth_bbo 就是这个角色）。
- Online invariants：never crossed、snapshot 对账、qty > 0。
- Property-based：随机生成合法 book 操作序列 → 随机扰乱投递 → 重建结果在非 stale 处等于真实 book。

**Q10 5,000 个 symbol、峰值流量？**
按 symbol（或 feed channel）分片，每个 shard 单线程顺序处理（保证确定性、不需要锁）；pin CPU core；避免 JSON（用二进制协议，零拷贝解析）；预分配内存；tick 数组代替 dict；只在需要时计算 top-K；把统计/存储放到旁路（另一个线程/进程通过 ring buffer 消费）。Python 做研究和正确性参考实现，生产热路径通常是 C++/FPGA。

## 3. 常见错误 / 扣分点
- 用 float 价格做 key，或 `int(px*100)`。
- 按 timestamp 排序；或者没意识到 trade 消息本身不改变 L2 book（重复扣量）。
- 发现 gap 后继续 apply，然后输出 crossed book 还不自知。
- 平均 spread 只算 event-weighted 不说明口径。
- 用全样本算 decile 再“验证”——look-ahead bias。
- 画图不带单位/标题；heatmap 不区分 bid/ask。
- 用 `df.iterrows()` 去 rebuild（极慢），或者一上来就写复杂类、30 分钟还没跑通第一版。

## 4. Speak-aloud scripts (English)

**Policy before code:**
> "Before I write the book, let me state my policy. I'll key everything on the exchange sequence number, not timestamps. Duplicates — anything below my next expected seq — get dropped. If a message arrives early, I buffer it for a small window. If the hole doesn't fill, I mark the book stale and wait for the next snapshot, which resets the book at sequence S; I then replay buffered messages above S. I'd rather publish 'stale' than a wrong book."

**Explaining the spread result:**
> "The event-weighted mean spread is 1.63 ticks but the time-weighted mean is 1.32. The difference is a sampling effect: during busy periods there are many more messages and the spread is also wider, so averaging per event over-weights those periods. If the question is 'what spread would I pay if I traded at a random moment', the time-weighted number is the right one."

**Explaining the imbalance result:**
> "I fitted decile cut-points on the first half only and evaluated on the second half. The probability that the next mid move is up rises monotonically from about 23% in the most ask-heavy decile to about 79% in the most bid-heavy decile, and out of sample the sign of imbalance gets the direction right 66% of the time. Part of this is mechanical — a thin ask queue is simply easier to deplete — so before calling it alpha I'd check it net of spread and latency."

## 5. 复杂度
- Rebuild：每条消息 O(1) 平均（dict 更新 + cached best）；删除 best 时 O(L) rescan。Reorder buffer O(B) 内存，B ≤ reorder window（stale 期间 ≤ snapshot 间隔）。
- Snapshot reset：O(L)。
- top-K：`heapq.nlargest` O(L log K)；有序结构 O(K)；tick 数组 O(K + 空 tick 数)。
- 统计部分都是向量化 pandas，O(N) 或 O(N log N)（merge_asof / qcut 排序）。
- 实测：纯 Python rebuild 约 1.4–1.6M msgs/s（已 parse 的 dict），`json.loads` 本身约 1.2M msgs/s —— 端到端瓶颈在 parse。
