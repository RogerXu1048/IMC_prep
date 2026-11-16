# F1 Study Guide — 从 gateway 日志算订单处理延迟

> 用法：先读 `problem.md`，然后按本指南一步一步从空文件写出 `solution.py`。
> 每一步都按同一个顺序练：先说（English）→ 做决策 → 写代码 → 自检 → 准备追问。
> 面试官看的是你**怎么拆问题、怎么做决定、能不能讲清楚 trade-off**，不是 API 记得多熟。

## 一句话题目
"给你一份多线程、有脏数据的 gateway 日志，算出每个订单在我们系统里花了多久，告诉交易台平均是多少、哪里出了问题。"

本质：**文本解析 → 按 id 配对事件 → 分组看分布 → 切片找 root cause**。这是 latency research 最常见的套路。

## 2 小时时间分配

| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify + 看原始数据 | 5 min | 问题清单、假设、`head` 看格式 |
| Step 1 Parse | 30 min | generator 解析器 + data-quality 报告 |
| Step 2 Pair events | 20 min | 每单一行的表 + status |
| Step 3 Statistics | 15 min | percentile 表 + "mean 不是答案" |
| Step 4 Diagnose | 30 min | 1 张图 + gw-3 周期卡顿 + ARCX 无响应 + RATE_LIMIT |
| Step 5 Scale | 10 min | 讨论 + mergeable histogram 小 demo |
| 收尾 | 10 min | 3–4 句结论 + 下一步 |

## 开场 5 分钟

**先问（English）：**
- "When you say *processing time*, do you mean time inside our gateway (IN→OUT), or including the exchange round trip?"
- "Are all timestamps on a line taken on the gateway host? Is it wall clock or a monotonic clock?"
- "Is the client order id unique per order, and could the same id appear on two gateways?"
- "Should I treat the market-open burst as part of normal behaviour, or report it separately?"
- "How big is the real file? Should I assume it doesn't fit in memory?"

**再说出假设（English）：**
> "I'll assume clid is unique per order and all timestamps on one gateway come from the same clock, so IN→OUT and OUT→ACK are both trustworthy differences. I'll pair events by clid rather than by file order, and I'll report percentiles, not just the mean."

然后 `head -20 gateway.log`，**大声描述你看到的格式**：时间戳、gateway、level、thread、事件、key=value。

---

## Step 1 — Parse（30 min）

**目标：** 一个 generator 逐行读文件，产出 `(ts_ns, gw, ev, mtype, clid, venue, reason)`，坏行计数不报错。

**先说什么：**
> "I'll write a streaming parser: an anchored regex for the fixed prefix, then split the body into key=value pairs. Anything I can't parse goes into a counter, so we know how much data we dropped."

**决策点与 trade-offs：**
- **Generator vs 一次读入**：选 generator，内存 O(1)/行。DataFrame 只装解析后的事件。代价是没法回看上一行，但我们本来就不该依赖上一行，因为文件是乱序的。
- **一个大 regex vs prefix regex + `split`**：选后者。`split` 是 C 层线性扫描，比带很多可选组的 regex 快，也更好读。代价是校验更松，所以要补一个 `REQUIRED` 字段检查。
- **时间戳**：6 位（µs）和 9 位（ns）混在一起，统一成**整数 ns**，不要用 float。epoch-ns 放进 float64 只能精确到约 256 ns，测 5 µs 的延迟会出错。
- **`strptime` 很慢**：15 分钟只有 900 个不同的秒，用 `lru_cache` 缓存"日期+秒 → epoch 秒"。
- **坏行**：traceback 续行（缩进、`Traceback`、`KeyError: ...`）和真正 malformed 的行（截断）**分开计数**，前者是正常日志内容。
- **截断行**：行被截断会丢掉最后的字段。所以每种事件要求必须有的 key（例如 IN NewOrder 要有 `qty`，OUT 要有合法的 `venue`），缺了就丢。宁可丢一行，也不要信半条记录。

**代码骨架：**
```python
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.(\d{6}|\d{9}) (gw-\d+) (\w+)\s+\[t\d+\] (.*)$")

def parse_lines(path, dq):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            dq["lines"] += 1
            m = LINE_RE.match(line.rstrip("\n"))
            if m is None:
                dq["traceback" if CONTINUATION_RE.match(line) else "malformed"] += 1
                continue
            date, hms, frac, gw, level, body = m.groups()
            tokens = body.split()
            ev = tokens[0]
            if ev not in ("IN", "OUT", "ACK", "REJ"):
                dq["error_lines" if level == "ERROR" else "heartbeat_stats"] += 1
                continue
            mtype = tokens[1] if ev in ("IN", "OUT") else None
            kv = dict(t.split("=", 1) for t in tokens if "=" in t)
            if not REQUIRED.get((ev, mtype), {"?"}) <= kv.keys(): ...   # malformed
            frac_ns = int(frac) if len(frac) == 9 else int(frac) * 1000
            yield epoch_s(date, hms) * 10**9 + frac_ns, gw, ev, mtype, kv["clid"], kv.get("venue"), kv.get("reason")
```
外层 `step1_parse` 做三件事：
1. `pd.DataFrame.from_records(generator)`；
2. 用 `np.diff(ts) < 0` 数文件里有多少行乱序；
3. `duplicated(["gw","clid","ev","ts_ns"])` 去掉完全重复的行。最后把 `ts_ns` 转成 `Int64`。

**自检：** 应该得到 `lines 273,800`、`events 269,737`、`heartbeat_stats 3,727`、`error_lines 46`、`traceback 276`、`malformed 14`、`out_of_order 1,325`、`duplicates 538`。用 `ts_ns % 1000 != 0` 检查哪个 gateway 用 ns 时间戳，答案是只有 `gw-4`。

**可能的追问：**
- *"Why not `pd.read_csv(sep=' ')`?"* 行的字段数不固定（不同事件的 key 不同），还有多行 traceback。read_csv 能更快地粗切，但之后的校验更麻烦。如果性能要求高，可以先用 read_csv 或 pyarrow 粗切，再用 `str.extract` 向量化解析。
- *"Duplicates with the same clid+event but a different timestamp?"* 这不是 logger 重试，而是真的冲突（比如重发），要单独报告，不能静默去重。
- *"1,325 lines out of order — does it matter?"* 只要按 clid 配对、按时间戳排序，就没影响。真正危险的是"上一行 IN 配这一行 OUT"这种写法。

---

## Step 2 — 每单一行（20 min）

**目标：** 把每个 clid 的 IN/OUT/ACK/REJ 时间戳变成列，算出 `internal_us`、`rtt_us` 和 `status`。

**先说什么：**
> "I'll pivot the events by clid, which is effectively a hash join, so file order doesn't matter. Then every order gets a status, because an order with no answer is a different and more dangerous state than a slow one."

**决策点与 trade-offs：**
- **配对方式**：`groupby(["clid","ev"]).ts_ns.min().unstack()`，在 pandas 里是 O(N) 哈希分组。流式版本用 `dict[clid] → t_in`，看到 OUT 就配对并删除（Step 5 会讲）。
- **为什么用 `min`**：去重之后理论上每个 (clid, ev) 只剩一行，`min` 只是保险。
- **`Int64` 而不是 float**：`unstack` 会产生缺失值。如果 dtype 是 int64，pandas 会把列转成 float，ns 精度就丢了。nullable `Int64` 能保留精度。
- **status 的优先级**：
  1. 没 IN → `INCOMPLETE_LOG`（日志丢了，不是订单的问题）；
  2. 没 OUT → `NOT_SENT`（内部错误，订单没发出去）；
  3. 有 ACK → `ACKED`；
  4. 有 REJ → `REJECTED`；
  5. 都没有 → `NO_RESPONSE`。
- **负延迟**：用 `assert` 检查。在真实系统里，出现负值说明配对或时钟有 bug，应该大声报错，而不是悄悄 `abs()`。

**代码骨架：**
```python
t = ev.groupby(["clid", "ev"]).ts_ns.min().unstack().reindex(columns=["IN", "OUT", "ACK", "REJ"])
o = t.join(ev.groupby("clid")[["gw", "mtype", "venue", "reason"]].first())   # first non-null
o["status"] = np.select([o.IN.isna(), o.OUT.isna(), o.ACK.notna(), o.REJ.notna()],
                        ["INCOMPLETE_LOG", "NOT_SENT", "ACKED", "REJECTED"], "NO_RESPONSE")
o["internal_us"] = ((o.OUT - o.IN) / 1000).astype(float)
o["rtt_us"] = ((o.ACK.fillna(o.REJ) - o.OUT) / 1000).astype(float)
assert not ((o.internal_us < 0) | (o.rtt_us < 0)).any()
```

**自检：** status 计数 `ACKED 88,481 / REJECTED 1,040 / NO_RESPONSE 288 / NOT_SENT 50 / INCOMPLETE_LOG 3`。NOT_SENT 中有 46 单对应 46 条 ERROR 行，其余 4 单是 OUT 行被截断了。

**可能的追问：**
- *"What does NO_RESPONSE mean for the business?"* 订单状态是**未知**，不是失败：它可能已经在交易所成交了。所以**不能直接重发**（可能成交两次）。正确做法是超时后标记为 pending-unknown，发 order status request 或用 drop copy 对账，风控按"可能已成交"计算敞口。
- *"Memory?"* 这一步是 O(订单数)。流式版本只需保存在途订单，O(in-flight)。

---

## Step 3 — 统计表（15 min）

**目标：** 按 gateway × type 给出 internal latency 的 percentile 表；按 venue 给出 RTT 的表，并把开盘时段单独拆出来。

**先说什么：**
> "Latency is right-skewed with a heavy tail, so I'll report p50 for the typical order and p99 and p99.9 for the tail, and keep the mean only as a signal: if the mean is far above the median, there's a tail."

**决策点与 trade-offs：**
- **mean vs percentile**：mean 适合做容量规划（总 CPU 时间 = mean × 订单数），但会被 1% 的 outlier 拉高。交易里亏钱的恰恰是尾部，最慢的那 1% 往往出现在行情最剧烈、机会最值钱的时候。
- **按什么切**：gateway × msg type（Cancel 本来就比 NewOrder 快），venue × (open / normal)。开盘的消息速率是平时的 5 倍，属于另一个 regime。混在一起算，会把 normal 的 p99 拉高，也会掩盖开盘时的恶化。
- **样本量**：p99.9 至少需要几千个样本才有意义。这里每组 5k–17k 个，够用。

**代码骨架：**
```python
def pct(s):
    s = s.dropna(); q = s.quantile([0.5, 0.99, 0.999])
    return pd.Series({"count": len(s), "mean": s.mean(), "p50": q[0.5], "p99": q[0.99], "p99.9": q[0.999], "max": s.max()})

internal = o.dropna(subset=["internal_us"]).groupby(["gw", "mtype"]).internal_us.apply(pct).unstack()
period = np.where(acked.t_out.dt.strftime("%H:%M:%S").between("14:30:00", "14:30:29"), "open", "normal")
rtt = acked.groupby(["venue", period]).rtt_us.apply(pct).unstack()
```

**自检：**
- NewOrder 的 p50 = 8.0 µs，Cancel = 5.0 µs，4 个 gateway 都一样。但 **gw-3 NewOrder 的 p99 = 1368.2 µs**（其他约 18 µs），mean = 37.3 µs。gw-3 整体 mean = 35.4 µs，median = 7.0 µs。
- RTT 的 normal p50：XNAS 34、ARCX 54、BATS 29 µs。开盘时 ARCX 的 p99 从 108.7 升到 295.2 µs。

**回答交易台的那句话（English）：**
> "Typical processing time inside the gateway is 8 µs for new orders and 5 µs for cancels, the same on all four gateways. The mean is misleading: gw-3's mean is 35 µs because about 1% of its orders take milliseconds. So I'd quote p50 and p99 per gateway."

---

## Step 4 — 诊断（30 min）

**目标：** 解释 gw-3 的尾部从哪来，找到无响应订单和 RATE_LIMIT 的模式，并用一张图让工程师一眼看懂。

**先说什么：**
> "gw-3's median is normal but its tail is 70 times worse, so something intermittent is happening. I'll look at *when* the slow orders occur. If the slow orders form a pattern in time, that points to a scheduled job rather than load."

**决策点与 trade-offs：**
- **怎么找周期**：取出慢单（> 500 µs）所在的整秒，算相邻秒的差，众数就是周期（10 s）。然后按周期"折叠"：`phase = t % 10s`。如果所有慢单都挤在同一个 phase，就是周期性任务（log flush、stats snapshot、cron、GC）在和 gateway 线程抢 CPU。也可以用 FFT 或自相关，但众数法简单、可解释。
- **阈值 500 µs**：正常单最慢约 30 µs，慢单在 1–4 ms，中间有很宽的空档，所以阈值不敏感。要说明这一点。
- **无响应订单**：按时间排序，相邻间隔 > 5 s 就切成新的 cluster（gap-based clustering，O(n)）。找到大 cluster 后要看 venue 和 gateway：只有 ARCX、4 个 gateway 都受影响，说明是交易所侧的问题，不是我们的。
- **图怎么画**：
  - latency 横跨 3 个数量级，必须用 log 轴；
  - 用 CCDF（`P(latency > x)`，log-log）比直方图更能看清尾部；
  - 第二个子图画折叠后的散点；
  - 第三个子图画每 5 s 的无响应订单数。

**代码骨架：**
```python
p99 = sent.groupby("gw").internal_us.quantile(0.99); worst = p99.idxmax()
t_in = w.IN.astype("int64"); slow = w.internal_us > 500
period_s = int(pd.Series(np.diff(np.unique(t_in[slow] // 10**9))).mode()[0])
phase_ms = (t_in % (period_s * 10**9)) / 1e6

nr = o[o.status == "NO_RESPONSE"].sort_values("OUT")
cluster_id = (nr.OUT.diff() > 5 * 10**9).cumsum()
clusters = nr.groupby(cluster_id).agg(n=("venue", "size"), start=("t_out", "min"), end=("t_out", "max"))
```
画图：`ax.step(x, 1 - np.arange(n)/n)` 画 CCDF；`ax.scatter(phase_ms, latency)`；`pd.crosstab(t.dt.floor("5s"), venue).plot(...)`。

**自检：**
- 各 gateway 的 p99：gw-1/2/4 约 17 µs，**gw-3 = 1240.5 µs**。
- gw-3 有 1.13% 的订单 > 500 µs，周期 = **10 s**，慢单都在每个周期的 **0–120 ms** 内。去掉这个窗口后，gw-3 的 p99 = 17.0 µs，和其他 gateway 一样。
- 无响应订单共 288 单，其中一个 cluster 有 **263 单，全是 ARCX，时间 14:36:41–14:37:01**（约 20 秒）。
- RATE_LIMIT 共 322 单，**全部来自 gw-2**，时间 14:30:00–14:30:29（开盘 burst）。

**可能的追问：**
- *"How would you fix gw-3?"* 先查那台机器上每 10 秒运行的任务（crontab、logrotate、监控 agent 的 snapshot），把 gateway 线程绑到隔离的 core 上（`isolcpus`、`taskset`），把后台任务移到别的 core。
- *"Is the ARCX gap our fault?"* 4 个 gateway 同时受影响，而且只有 ARCX，所以是交易所或线路的问题。应该通知交易台，并自动暂停往 ARCX 发单。
- *"RATE_LIMIT?"* gw-2 在开盘时超过了交易所的消息速率限制。可以在 gateway 侧做 throttling（token bucket），或者把流量分到其他 session。

---

## Step 5 — 规模化（10 min）

**目标：** 讲清楚 200 GB/天、40 台机器时怎么算；再用几行代码证明"不能平均 p99"。

**先说什么：**
> "Percentiles don't add up. Each gateway should ship a mergeable sketch, like an HDR-style log-bucket histogram, and the central node adds the bucket counts and reads the quantile from the merged histogram."

**决策点与 trade-offs：**
- **Log-bucket histogram**：
  - 相对误差由 bucket 宽度决定（这里是 1 µs 到 100 ms 分 500 个 bucket，每个约 2.3%）；
  - 内存是 O(bucket 数)，和样本数无关；
  - merge 就是把 count 相加；
  - 代价是有量化误差，而且不能找回原始样本。其他选择有 t-digest、DDSketch。
- **架构**：
  - 每台 gateway 本地流式解析，每秒或每分钟输出一个 histogram 和异常事件（无响应、reject、ERROR）；
  - 按 gateway 分区（天然独立），中心节点做 merge 后存入时序库；
  - 更好的做法是热路径直接输出二进制事件，不要先写文本日志再解析回来。

**代码骨架：**
```python
bins = np.logspace(0, 5, 501)
merged = sum(np.histogram(g.internal_us, bins)[0] for _, g in sent.groupby("gw"))
merged_p99 = bins[np.searchsorted(np.cumsum(merged) / merged.sum(), 0.99) + 1]
wrong_avg = sent.groupby("gw").internal_us.quantile(0.99).mean()
```

**自检：** 全局精确 p99 = **18.0 µs**，merge 后的 histogram 给出 **18.2 µs**（误差在一个 bucket 宽度内），而 4 个 gateway p99 的平均值 = **322.9 µs**，完全错误。同时注意：全局 p99 看不出 gw-3 的问题，因为它的慢单只占全部订单约 0.28%。所以**监控必须按 gateway 切片**。

---

## 收尾（10 min）

**English script：**
> "To summarise: typical processing time inside our gateways is 8 µs for new orders and 5 µs for cancels, and it's consistent across gateways. Two things are wrong. First, gw-3 stalls for about 120 ms every 10 seconds, which pushes its p99 from 17 µs to over a millisecond; that looks like a periodic job on that host competing for the core. Second, ARCX stopped answering for about 20 seconds at 14:36:41, leaving 263 orders in an unknown state, and gw-2 hit the exchange's rate limit during the open. Next I'd check what runs every 10 seconds on gw-3, reconcile those 263 orders against the drop copy, and add per-gateway p99 and no-response alerts."

**时间更多的话：**
- 写完整的流式配对版本（`dict` + TTL 清理）；
- 加 unit tests；
- 按 symbol 看 latency；
- 检查 coordinated omission。

---

## 性能追问（网上那 6 个问题，放在本题的语境里回答）

**1. 内存里用什么数据结构？**
- 解析阶段用 generator 加 tuple，不要每行建 dict 或对象。
- 配对用 `dict[clid] → t_in`，平均 O(1)。
- 统计用 pandas 的列式存储：ts 用 Int64；gw、venue 这类低基数的列用 `category`（省内存，groupby 也更快）。
- 流式聚合用 log-bucket histogram，内存 O(bucket 数)。

**2. 现代 Python 的内存管理和错误处理？**
- 用 `with open(...)` 这样的 context manager，保证文件或 socket 一定会关闭。
- 用 generator 惰性处理，不把 200 GB 一次读进内存。
- 只捕获**预期中的**异常（比如 `ValueError` 记为 malformed），并且要计数、要报告。不要写 `except Exception: pass`。
- 不变量用 `assert` 检查，或者显式 raise。
- CPython 用引用计数加循环 GC 管理内存。大对象及时 `del`；数值数据用 numpy 或 pandas 的连续数组，开销比 Python 对象小一个数量级。

**3. 怎么验证正确性？**
- **Unit test**：每种行格式各一行，包括截断行、ns 时间戳、traceback 续行，检查解析结果。`pct` 用手算的小数组验证。status 的每个分支各一个 case。
- **E2E test**：一个手写的 20 行小日志文件（含乱序和重复），期望的 per-order 表写死在测试里。
- **Regression test**：完整数据的输出存成 golden file，以后改代码就和它比对。
- **Property test**：把输入行随机打乱以后，结果必须不变，因为配对不依赖文件顺序。

**4. 交易场景关心什么指标？**
- **Latency 分位数**：p50、p99、p99.9、max，以及 jitter（p99 − p50）。
- **Throughput**：每秒订单数、解析速度（这里约 38 万行/秒）。
- 如果下游有 consumer，还要看 **handoff latency**：从入队到出队的时间。它会受队列长度、批处理大小和 GIL 切换影响。
- **Tail 和 throughput 之间有 trade-off**：batching 能提高吞吐，但会增加单条消息的延迟。

**5. 怎么测热点（时间和内存）？**
- **时间**：
  - `cProfile` 是 deterministic（tracing）profiler，每次函数调用都记录，结果准确但开销大。本题 step 1 在 cProfile 下从 0.72 s 变成 1.2 s，而且会放大小函数的占比。
  - `py-spy` 是 sampling profiler，定时采样调用栈，开销很小，可以 attach 到线上进程，代价是采样误差。
  - Hardware trace（`perf`、Intel PT）能看到 cache miss 和指令级的热点。
  - 本题 profile 的结果：大部分时间花在 `parse_lines` 自身的 Python 字节码上（regex match、split、dict 构造）。
- **内存**：`tracemalloc` 看分配峰值和分配位置；`memray` 看完整的分配时间线。

**6. 怎么并发？**
- 这是 CPU-bound 任务。因为有 GIL，threads 没用（3.13 的 free-threaded build 例外）。
- 用 **multiprocessing**，按文件或 gateway 分 shard（天然独立）；每个进程输出 histogram 和计数，最后 merge（map-reduce）。
- 如果是单个大文件，按字节范围切块，切点对齐到换行符。但跨块的订单需要在 merge 阶段配对。
- asyncio 只对 I/O-bound 有用，比如从远程存储下载日志。

**工作循环：** correctness（先写一个简单、明显正确的版本，加测试）→ benchmark（端到端测 lines/s、内存峰值）→ microbenchmark（只测热点，例如 regex vs split、`strptime` 加不加缓存）→ implement 优化 → 回到 correctness，保证结果和 golden file 一致。每次只改一处。

---

## 常见坑
- 用"上一行"配对，结果被 1,325 行乱序坑了。
- 没去重，重复的 OUT 导致重复计数。
- 时间戳转成 float（`pivot_table` 或 `unstack` 遇到缺失值会自动变成 float），丢了几百 ns 的精度。
- 6 位和 9 位小数没有统一，gw-4 的延迟差了 1000 倍。
- 只报 mean，或者只报全局数字，结果看不到 gw-3。
- 直方图用线性坐标，所有数据挤在最左边的一根柱子里。
- 把 NO_RESPONSE 当成 REJECTED，或者直接丢掉。
- 把各 gateway 的 p99 取平均。

## 练习方法
1. **第 1 轮**：照着本指南逐步写，每步跑完核对"自检"里的数字。
2. **第 2 轮**：只看 `problem.md` 和函数名（`step1_parse`、`step2_orders`、`step3_stats`、`step4_diagnose`、`step5_merge_p99`），自己写出函数体。
3. **第 3 轮**：空文件，计时 2 小时，全程用英文出声说"我要做什么、为什么、trade-off 是什么"。卡住时先说出你的思路，再请"面试官"帮你查语法。
