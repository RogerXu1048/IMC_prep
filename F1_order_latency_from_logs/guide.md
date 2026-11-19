# F1 Study Guide — 从 gateway 日志算订单延迟
## 一句话题目
给一份多线程、有脏数据的 gateway 日志，算出每个订单在我们 gateway 里花了多久、交易所多久回复，告诉交易台"典型值是多少、哪里出了问题"。套路：**regex 解析 + 审计 → 按 clid 配对 → 分组看分位数（先拆掉 confounder）→ 按时间切片找 root cause**。

## 2 小时时间分配
| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify + `head` 看原始数据 | 5 min | 3 个问题、假设、看清行格式 |
| Step 1 Load / audit | 30 min | regex 循环 + 审计计数 |
| Step 2 每单一行 | 15 min | `pivot_table` + status |
| Step 3 统计 | 20 min | 分位数表、拆出开盘 burst、图 1 |
| Step 4 诊断 | 35 min | 周期卡顿、无响应、RATE_LIMIT、图 2 |
| 重跑 + 核数字 + 收尾 | 15 min | 换目录重跑，2 分钟结论 + limitations |
| Step 5 规模化（加分） | 有时间写 15 min，否则口头讲 | 流式解析、可合并的直方图、多进程 |

## Clarifying questions
- "By processing time, do you mean time inside our gateway (IN→OUT), or including the exchange round trip?"
- "Are IN, OUT and ACK all stamped by the same host clock, and is it wall-clock or monotonic?"
- "Should I treat the market-open burst as normal behaviour, or report it separately?"

## Step 1 — Load / parse / audit
- **目标：** 一个 regex + 简单循环，把每条订单事件变成 `(ts_ns, gw, ev, mtype, clid, venue, reason)`；不 match 的行按类型计数，不报错。
- **先说什么：** "I'll parse every line with one anchored regex, count each kind of line that doesn't match, and keep timestamps as integer nanoseconds."

**决策与 trade-off：**
- 一个 regex，两头锚定（`^…$`）：被截断的行自然 match 失败，计入 malformed 并丢弃，宁可丢一行也不信半条记录。代价是 regex 很严格，格式一变就会大量失败，所以**一定要打印 skipped 计数**，malformed 突然变多就先查 regex。
- 时间戳：6 位（µs）和 9 位（ns）小数混在一起，用 `frac.ljust(9, "0")` 统一成整数 ns since midnight，不用 float。只解析时分秒，因为文件只有一天（这是 limitation）。
- 重复行直接丢（logger 重试写入）；乱序行保留，因为后面按 clid 配对，不依赖文件顺序。打印"最多倒退多少 ms"，说明这是线程交错，不是时钟跳变。

**关键代码：**
```python
EVENT_RE = re.compile(r"^\S+ (\d\d):(\d\d):(\d\d)\.(\d{6}|\d{9}) (gw-\d) +\w+ +\[t\d\] (IN|OUT|ACK|REJ) +"
                      r"(?:(NewOrder|Cancel) )?clid=(\S+) (?:venue=(XNAS|ARCX|BATS)|reason=([A-Z_]+)|.*=\w+)$")
    with open(LOG) as f:
        for line in f:
to_ns = lambda h, mi, s, frac: ((int(h) * 60 + int(mi)) * 60 + int(s)) * NS + int(frac.ljust(9, "0"))
            if m := EVENT_RE.match(line):
                rows.append((to_ns(*m.groups()[:4]), *m.groups()[4:]))   # DECISION: int ns, us padded. Never float.
            elif not line[:1].isdigit():       # indented lines under an ERROR
                dq["traceback"] += 1
            elif " ERROR " in line:
                dq["ERROR"] += 1
            elif " HB " in line or "session stats" in line:
                dq["HB/stats"] += 1
            else:                              # truncated line: drop it rather than trust half a record
                dq["malformed"] += 1
```

**自检：** 273800 行 → 269737 条订单事件；skipped：malformed 14、traceback 276、ERROR 46、HB/stats 3727；丢掉 538 条 exact duplicates；保留 1325 条乱序行（最多倒退 70.2 ms）；ns 时间戳只有 `['gw-4']`。

## Step 2 — 每单一行
- **目标：** 把每个 clid 的 IN/OUT/ACK/REJ 时间戳变成列，算 `internal_us`、`rtt_us` 和 `status`。
- **先说什么：** "I'll pivot the events by clid, so file order doesn't matter, and give every order a status, because an order with no answer is a different and more dangerous state than a slow one."

**决策与 trade-off：**
- `pivot_table` 遇到缺失事件会填 NaN，int64 列会被悄悄转成 float64。所以先 `astype({"ts": "Int64"})`（nullable int），ns 精度不丢。
- status 的顺序：有 ACK → acked；有 REJ → rejected；没有 OUT → not sent；都没有 → no response。先看 ACK 再看 OUT：OUT 行被截断、但交易所回了 ACK 的单，仍然算 acked。
- 负延迟用 `assert`：出现负值说明配对或时钟有 bug，要大声报错，不要悄悄 `abs()`。

**关键代码：**
```python
    t = ev.astype({"ts": "Int64"}).pivot_table(index="clid", columns="ev", values="ts", aggfunc="min")
    o = t.join(ev.groupby("clid")[["gw", "mtype", "venue", "reason"]].first())
    o["internal_us"] = ((o.OUT - o.IN) / 1e3).astype(float)            # inside our gateway, one host clock
    o["rtt_us"] = ((o.ACK.fillna(o.REJ) - o.OUT) / 1e3).astype(float)  # wire + venue
    o["status"] = np.select([o.ACK.notna(), o.REJ.notna(), o.OUT.isna()], ["acked", "rejected", "not sent"],
                            default="no response")
```

**自检：** acked 88488、rejected 1040、no response 288、not sent 46。not sent 46 正好等于 Step 1 的 46 条 ERROR 行（handler exception，订单没发出去），这是一个交叉验证。

## Step 3 — 统计
- **目标：** internal latency 按 gateway × msg type 的 p50 / p99 / mean；venue round trip 按 venue × (open / normal)。
- **先说什么：** "Latency is right-skewed, so I'll report p50 for the typical order and p99 for the tail; the mean only tells me whether a tail exists."

**决策与 trade-off：**
- mean vs percentile：mean 适合容量规划（总时间 = mean × 订单数），但会被 1% 的慢单拉高；交易里亏钱的恰恰是尾部。
- **Confounder = 开盘 burst**：消息量约是平时的 5 倍，交易所也回得更慢。用"每秒订单数 > 3 × 中位数"从数据里找出 burst，再核对它就是 14:30 UTC 美股开盘。混在一起算，会抬高 normal 的 p99，也会让开盘时下单多的 venue 看起来更差。
- RTT 只用 ACK（REJ 走的路径不同），表里保留 `n` 列：p99 至少要几千个样本才稳。

**关键代码：**
```python
    sent = o.dropna(subset=["internal_us"])
    g = sent.groupby(["gw", "mtype"]).internal_us
    internal = g.quantile([0.5, 0.99]).unstack().assign(mean=g.mean()).round(1)
    per_s = (o.OUT.dropna() // NS).value_counts()                    # orders sent in each second
    burst = per_s[per_s > 3 * per_s.median()]   # DECISION: split out this burst (the confounder). CHECK: = US open?
    acked = o[o.status == "acked"]
    g = acked.groupby(["venue", np.where((acked.OUT // NS).isin(burst.index), "open", "normal")]).rtt_us
```

**自检：**
- 4 个 gateway 的 p50 都是 NewOrder 8.0 µs、Cancel 5.0 µs。gw-3 NewOrder 的 p99 = 1368.2 µs、mean = 37.3 µs；其他 gateway 的 NewOrder p99 是 17.9–18.0 µs、mean 8.5 µs。
- Burst = 14:30:00–14:30:29，431 单/秒 vs 平时 89 单/秒。
- RTT p50 从 normal 到 open：ARCX 54.0 → 82.0、BATS 29.0 → 43.0、XNAS 34.0 → 51.0 µs；ARCX 的 p99 从 108.7 升到 295.2 µs。

**回答交易台：** "A typical order spends 8 µs inside our gateway, 5 µs for a cancel, on all four gateways. The mean is misleading: gw-3's mean for new orders is 37 µs because about 1% of its orders take milliseconds."

## Step 4 — 诊断
- **目标：** (a) gw-3 的尾部从哪来；(b) 无响应订单是不是一次事件、哪个 venue、什么时候；(c) RATE_LIMIT 是谁、什么时候。
- **先说什么：** "gw-3's median is normal but its p99 is two orders of magnitude worse, so something intermittent is happening; I'll look at *when* the slow orders occur."

**决策与 trade-off：**
- 找周期：取慢单（> 500 µs）所在的整秒，相邻差的众数就是周期（代码里 CHECK 它等于 `CYCLE_S`），再按周期折叠 `phase = t % 10 s`。慢单全挤在同一个 phase → 定时任务在抢 CPU，不是负载。阈值不敏感：图上正常单都在 ~30 µs 以下，慢单都在 1 ms 以上。
- 小心 confounder：14:30:00 正好也是一个 10 s 周期的起点，只看开盘会误以为是开盘负载；折叠后 15 分钟里每个周期都有，排除了开盘。
- 无响应：按时间排序，间隔 > 5 s 就开一个新 incident（gap-based clustering，O(n)）。看 incident 里有几个 venue、几个 gateway：只有 ARCX、4 个 gateway 都受影响 → 交易所侧的问题，不是我们某台 gateway。

**关键代码：**
```python
    w = o[(o.gw == worst) & o.internal_us.notna()].assign(t=lambda d: d.IN.astype("int64"))
    phase_ms, slow = (w.t % (CYCLE_S * NS)) / MS, w.internal_us > SLOW_US      # phase = ms into the cycle
    stall_ms = phase_ms[slow].max()
    nr = o[o.status == "no response"].assign(t=lambda d: d.OUT.astype("int64")).sort_values("t")
    nr["incident"] = (nr.t.diff() > 5 * NS).cumsum()                   # DECISION: a > 5 s gap starts a new incident
    big, rl = nr[nr.groupby("incident").t.transform("size") >= 10], o[o.reason == "RATE_LIMIT"]
```

**自检：**
- gw-3：1.13% 的订单 > 500 µs，每 10 s 一次，全部在周期的前 120 ms；窗口外 gw-3 的 p99 = 17.0 µs，和别的 gateway 一样。
- 无响应共 288 单；其中 263 单集中在 14:36:41 起的 20 s 内：ARCX 262、XNAS 1，来自 4 个 gateway。
- RATE_LIMIT：`{'gw-2': 322}`，全部在 14:30:00–14:30:29（开盘 burst）。只有 gw-2 → 查 gw-2 的 session 限额或 throttle 配置。

## Step 5 — 规模化：流式解析、可合并的直方图、并行（加分）
有时间就写（约 15 min），没时间就口头讲。面试官问"200 GB/天怎么办"时，这一节就是答案。
- **目标：** 生产日志约 200 GB/天、40 个 gateway，不能整份读进 DataFrame。改成每个 gateway 一个 worker 流式读，内存只存还没配对的订单；每个 worker 返回一个固定大小的 log 直方图；中心节点把直方图加起来，得到全局 p99。
- **先说什么：** "A DataFrame of every event won't scale to 200 GB a day. I'd stream each gateway's log, keep only the orders that are still open, and drop each latency into a log-spaced histogram. Histograms add up across gateways and days; percentiles don't."

**决策与 trade-off：**
- **缓冲只放未配对的单。** `open_` 是以 clid 为 key 的 dict。先到的一端存进去（乱序时 OUT 可能比 IN 先写进日志），另一端到了就算出延迟，再删掉这条。内存等于同时在途的订单数，和文件大小无关。代价是配对完成后才出现的重复行会留下"孤儿"条目（这里有 232 个，包括 46 个没发出去的单）。生产里要按 TTL（比如 5 s）清理，不然内存会慢慢涨。
- **优化。** 先用便宜的 substring 判断（`f" {gw} " in line`）筛掉别的 gateway，再跑 regex。直方图的下标直接用 `log10` 算，是 O(1)，不用排序，也不存原始值。每个 bin 约宽 5%，所以 p99 的误差不超过一个 bin（实测 18.2 µs，精确值 18.0 µs）。
- **并发。** regex 解析是 CPU-bound，而 Python 有 GIL，所以用**进程**，不用线程。每个进程只处理一个 gateway，进程之间不共享状态，最后只传回 200 个整数，这就是 map-reduce。
- **先测量再说加速。** 在这份 24 MB 的文件上，4 个进程（0.6–0.8 s）并不比单进程（0.6 s）快：每个进程启动都要重新 import pandas，而且这里每个 worker 都要把整份文件扫一遍。等到每个 gateway 一个文件、每个文件几个 GB 时，启动开销可以忽略，速度才接近按核数成倍提升。
- **p99 不能取平均。** 4 个 gateway 的 p99 平均下来是 343 µs，但真实的全局 p99 是 18 µs：gw-3 的慢单只占全部订单的 0.28%。percentile 不能相加，也不能平均；直方图（或者 t-digest、HDR histogram）可以相加。

**关键代码：**
```python
            if f" {gw} " in line and (m := EVENT_RE.match(line)) and m[6] in ("IN", "OUT"):  # cheap test first
                ts, ev, clid = to_ns(*m.groups()[:4]), m[6], m[8]
                first = open_.setdefault(clid, (ev, ts))     # OUT can be logged before IN: keep whichever is first
                if first[0] != ev:                           # pair complete -> one sample, free the memory
                    us = (ts - first[1] if ev == "OUT" else first[1] - ts) / 1e3
                    h[min(max(int(PER_DEC * math.log10(max(us, 1))), 0), NB - 1)] += 1   # O(1) log bin
                    del open_[clid]
    with ProcessPoolExecutor() as ex:        # DECISION: processes, not threads: regex parsing is CPU-bound (GIL)
        res = dict(zip(gws, ex.map(gw_hist, gws)))
    merged = sum(h for h, _ in res.values())   # DECISION: ship histograms and ADD them; p99s cannot be averaged
```

**自检：**
- 合并后的直方图 p99 ≤ 18.2 µs（精确值 18.0 µs）。直方图里共 89,809 单，和 Step 2 配对出来的单数完全一致，代码里有 assert。
- 把 4 个 gateway 的 p99 取平均是 343 µs，这是错的。
- 最后还留在缓冲里的是 232 条；单进程 0.6 s，4 个进程 0.6–0.8 s。

**实时告警（只讲，不写）：**
- 每个 gateway 每 10 s 往中心发一个直方图。
- p99 告警要求"连续 3 个窗口超过阈值"，这样单个 outlier 不会把人叫起来。
- no response 按 venue 计数：几秒内超过 N 单就告警，并触发对账（reconcile），而不是自动重发。

> "On this file, four processes weren't faster than one, because process start-up and re-reading a shared 24 MB file dominate. With one file per gateway and gigabytes in each, the work per process dwarfs the start-up, so it would scale with cores. I'd measure that rather than assume it."

## 图怎么讲
**图 1 `f1_latency_tail.png`：按 gateway 的 1 − ECDF（log-log）。** 为什么：要比较 4 个完整分布，而且重点在尾部；4 个 histogram 叠在一起会互相遮挡，1% 的尾巴在线性轴上也看不见。log y 把 1%、0.1% 拉开。
> "The four curves overlap down to about 1%, then gw-3 flattens out into milliseconds: same typical latency, very different tail."

**图 2 左 `f1_diagnosis.png`：gw-3 延迟 vs 周期内的 ms（散点，log y）。** 为什么：按怀疑的周期折叠时间，"随机"的尖峰就变成一根柱子；每个点是一单，能直接看出"每一个"慢单都在窗口里。
> "Every slow gw-3 order sits in the first 120 ms of a 10-second cycle and the rest of the cycle is clean, so this is a scheduled job, not load."

**图 2 右：无响应订单时间线（每单一根竖线，y = venue，x = HH:MM）。** 为什么：event raster 同时回答"哪个 venue"和"什么时候"；按分钟计数的柱状图看不出它只持续了 20 s，也看不出其余的都是零星单个。
> "Unanswered orders are scattered singles, except one solid block on ARCX starting at 14:36:41."

## 结论与 limitations（English script）
> "A typical order spends 8 µs inside our gateways, 5 µs for a cancel, the same on all four, so I'd report p50 and p99 per gateway rather than one mean. gw-3 stalls for about 120 ms every 10 seconds; outside that window its p99 is 17 µs like the others, which points to a periodic job on that host competing for the core. ARCX stopped answering for about 20 seconds from 14:36:41 on all four gateways, leaving 262 orders in an unknown state that must be reconciled, not resent, and gw-2 alone hit the venue's rate limit in the opening burst. For the whole fleet, I'd ship per-gateway histograms and add them: averaging the four p99s gives 343 µs when the true figure is 18. The limitations: this is one 15-minute window on one day, log time is when the line was written rather than when the event happened, and the stall's cause is inferred from timing, not proven. Next I'd check gw-3's cron and perf data and add per-gateway p99 and no-response alerts."

## 常见坑
- 用"上一行"配对 IN 和 OUT，被 1325 行乱序坑。
- 时间戳变成 float（`pivot_table` 补 NaN 时自动转），或者 6 位和 9 位小数没统一，gw-4 的延迟差 1000 倍。
- 只报 mean 或只报全局数字，看不到 gw-3；开盘 burst 和 normal 混在一起比较 RTT。
- 看到 14:30:00 附近的慢单就归因于开盘，没有按 10 s 周期折叠去验证。
- 把 no response 当成 rejected，或者直接丢掉；把 not sent 和 no response 混在一起。
- 把各个 gateway 的 p99 取平均当作全局 p99；或者没测量就说"多进程一定更快"。

## 练习方法
1. **第 1 轮：** 照着本指南逐步写，每步跑完对照"自检"里的数字。
2. **第 2 轮：** 只看 `problem.md`，自己写 `load / per_order / stats / diagnose / main`，写完再和 `solution.py` 对比。Step 5（`gw_hist / scale_out`）至少口头讲一遍。
3. **第 3 轮：** 空文件，计时 2 小时，全程用英文出声讲"做什么、为什么、trade-off"，最后 2 分钟讲结论和 limitations。
