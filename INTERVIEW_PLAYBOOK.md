# 面试答题 Playbook：一套模版 + 5 张题卡

**怎么背**
- 先背第 1 部分的模版（口诀和英文句式）。
- 每张题卡只背四样：**思维链、分块表、决策表、收尾**。代码骨架看懂即可，不用背。
- 题卡顺序按练习优先级：F3 → F1 → F4 → F2 → F5。

---

## 1. 通用模版

### 口诀：**问 → 讲 → 查 · 建 · 量 · 控 · 诊 → 报**

| 步 | 做什么 | 一句话 |
|---|---|---|
| **问** | Clarify：最多 3 个问题，加上规模和格式 | 先弄清数据含义、时间戳、目标指标 |
| **讲** | 先讲方案，再写代码 | "I'll break this into N steps…" |
| **查** | Audit 原始数据 | 重复、乱序、缺失、坏行、单位：先数清楚，再决定怎么处理 |
| **建** | 建分析表 | 一个实体一行（一笔单、一个包、一次尝试），按 key 配对，不按行顺序 |
| **量** | 选对指标 | 看分布不看均值（p50/p99）；比例要带置信区间 |
| **控** | 控制混杂因素（confounder） | 同类比同类：配对、分层、对照组、切时间窗口、换成同样的构成 |
| **诊** | 找原因：什么时候、在哪、为什么 | 按时间、机器、负载切开，用图把模式找出来 |
| **报** | 结论 | 答案 → 证据 → limitations → 下一步 |

每个 block 都按同样的 4 句话讲：**目的 → 方法 → 决定（trade-off）→ 怎么验证**。

### 1.1 开场：千万不要问"我要做什么"，先给出自己的理解
> "Let me restate the goal: we want to know **[question]** from **[data]**. Three quick questions before I start: …
> If not, I'll assume **[assumption]**.
> My plan has four steps: first, audit the raw data; second, build one row per **[order / packet / attempt]**; third, the key comparison, where I'll control for **[confounder]**; fourth, diagnose the cause with a couple of plots. I'll finish with conclusions and limitations. Does that sound reasonable?"

**Clarify 的四类问题**（每类挑一个，总共不超过 3 个）

| 类别 | 例句 |
|---|---|
| 数据含义 | "What does one row represent? Is `qty` the new total or a delta?" |
| 测量 | "Who stamps each timestamp, the same clock? What resolution?" |
| 目标 | "Do we care most about the typical case (median), the tail (p99), or a rate?" |
| 规模和格式 | "Roughly how big is it, and what format: CSV, Parquet or a text log? Is this a one-off offline analysis or something that will run live?" |

规模决定写法：几百万行以内就用 pandas 向量化；更大就分块读、流式处理、按文件并行。

### 1.2 写代码时：先讲逻辑，再请求语法提示
> "The logic is: group by line and date, then take the 99th percentile of latency. I don't write pandas every day, so I may not remember the exact syntax for a custom aggregation. Could you give me a hint? The idea is what matters here."

> "I'll sketch this in pseudo-code first, then fill in the syntax."

### 1.3 每个决定都用同一个句式讲 trade-off
> "I'm choosing **X** over **Y** because **[reason]**. The cost is **[downside]**, so I'll check **[validation]**."

### 1.4 画图前先说为什么选这张图
> "I'll plot **[chart]** because the question is **[comparison / distribution / trend]**. If my hypothesis is right, we should see **[pattern]**."

画完把结论写进标题。

### 1.5 收尾（30–45 秒）
> "**Answer:** yes or no, with one number.
> **Why the naive view is wrong:** [the confounder].
> **Evidence:** [the fair comparison and its numbers].
> **Other findings:** [one or two].
> **Limitations:** [two or three].
> **Recommendation / next step:** …"

### 1.6 延迟问题先分诊：外部负载，还是我们自己的系统？

| 现象 | 多半是 | 例子 |
|---|---|---|
| 随负载上升，所有线路或机器一起变慢 | 市场活跃度、容量（排队） | F3 拐点 70–80 msgs/100 µs；F1 开盘 burst |
| 只有一台机器或一个 session，和负载无关，有周期 | 我们自己的系统（定时任务、配置） | F1 gw-3 每 10 s 卡一次；F5 的 Nagle |
| 只有一个 venue，所有 gateway 一起受影响 | 交易所那边 | F1 ARCX 20 s 不回 |
| 我们的延迟没变，结果却变了 | 市场或竞争对手 | F4 EX3 第 16 天 |

### 1.7 控制变量清单：要控制什么，怎么控制

| 混杂因素 | 题目 | 控制手段 |
|---|---|---|
| 负载、市场活跃度 | F3、F1 | **分层**：`pd.cut` 分负载桶，桶内再比；开盘时段单独看 |
| 时段（开盘） | F1 | 按时间切开，单独报 |
| 构成（venue mix、消息类型） | F4、F1 | 分组看；**标准化**：换成同样的构成 |
| 路径、机器（line、gateway、session） | F3、F5 | **配对**：同一个包、同一笔单；**对照组**：A 线、s4 |
| 版本或事件（发版、竞争者） | F4、F5 | 先画每日图找断点，**不跨断点合并** |
| 时钟偏差、漂移 | F3 | 只信同一个时钟下的差值（B − A） |
| 幸存者偏差（丢包、无回复） | F3、F1 | 单独数丢掉的样本；可靠性和延迟一起报 |
| look-ahead | F2 | 前半段定参数，后半段检验 |

### 1.8 七个会导致错误结论的坑
1. 只报 mean：会被 1% 的慢单拉偏。
2. 按行顺序配对：日志会乱序。
3. 直接比较前后两天：负载、构成可能不一样。
4. 跨越结构断点合并数据。
5. 只分析"活下来"的样本：丢包、无回复的样本被排除了，结果看起来比实际好。
6. 把各机器的 p99 取平均：percentile 不能平均，要合并直方图。
7. 用全样本定阈值再在全样本上评估：这是 look-ahead。

---

## 2. 工具箱：要用的 pandas 和 matplotlib 函数

```python
import re, json                      # 文本日志 / JSON lines
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
```

| 用途 | 函数（只记名字和作用） | 题目 |
|---|---|---|
| 读数据 | `pd.read_parquet`、`pd.read_csv`；逐行 `re.match` 或 `json.loads` 加 try | 全部 |
| 重复 | `duplicated(keys)`、`drop_duplicates(keys)` | F1–F4 |
| 乱序 | `groupby(keys).seq.cummax()`：比之前最大值还小，就是晚到的 | F2、F3 |
| 缺号 | 排序后 `groupby(keys).seq.diff() - 1`，大于 0 就是缺了几个 | F2、F3 |
| 把组的统计值填回每一行 | `groupby(keys).col.transform("size")` | F3、F1 |
| 一个实体一行 | `pivot_table(index=id, columns=event, values=ts)` | F1 |
| 配对 | `merge(a, b, on=keys)` | F3、F2 |
| 时间上最近的匹配 | `pd.merge_asof(..., by=..., direction="backward" 或 "forward")` | F5 |
| 分组统计 | `groupby(...).agg(p50="median", p99=lambda s: s.quantile(.99), n="size")` | 全部 |
| 宽表 | `.unstack()`：把一层 index 转成列 | 全部 |
| 固定边界分桶 | `pd.cut(x, bins)` | F3 |
| 等样本量分桶 | `pd.qcut(x, 10)` | F4、F2 |
| 构成比例 | `pd.crosstab(a, b, normalize="columns")` | F4 |
| 按条件打标签 | `np.select([cond1, cond2], [label1, label2], default)` | F1 |
| 滑动窗口计数 | `np.searchsorted`（见下面） | F3 |
| 周期折叠 | `t % period` | F1 |
| 事件聚成 incident | `(t.diff() > gap).cumsum()` | F1 |
| 找第一个超过阈值的位置 | `(curve > threshold).idxmax()` | F3 |

**画图：问题 → 图 → 函数**

| 问题 | 图 | matplotlib |
|---|---|---|
| 分布形状（有没有双峰） | log 分桶直方图 | `ax.hist(x, bins=np.logspace(0, 4, 50)); ax.set_xscale("log")` |
| 比较各组的尾部 | ECDF，或 log-log 上的 1 − ECDF | `x = np.sort(v); ax.plot(x, np.arange(1, len(x)+1)/len(x))` |
| 随时间变化，标出事件 | 折线加阴影 | `ax.plot(t, y); ax.axvspan(t0, t1, alpha=.3)` |
| 比较几组 | 柱状图、带误差线的点图 | `df.plot.bar()`；`ax.errorbar(x, y, yerr=…)` |
| 比例随时间变化，带 CI | 折线加 CI 带 | `ax.fill_between(x, lo, hi, alpha=.2)` |
| 两个变量的关系 | 散点或 hexbin | `ax.scatter(x, y, s=2, alpha=.2)`；`ax.hexbin(x, y, gridsize=50, bins="log")` |
| 报价随时间变化 | step 图 | `ax.plot(t, bid, drawstyle="steps-post")` |
| 事件发生在什么时候、哪个类别 | event raster | `ax.scatter(t, venue, marker="|")` |

### 2.1 滑动窗口：`np.searchsorted` 到底在做什么
**问题**：每个包到达时，过去 100 µs 内交易所一共发了多少个包？这个数就是"负载"，F3 用它来控制混杂因素。

**做法**：先把所有包按时间排好序。对第 i 个包，窗口是 (tᵢ − 100 µs, tᵢ]。
- `j = searchsorted(t, tᵢ − 100, side="right")` 用二分查找，找到窗口里第一个包的位置。
- 窗口里的包数 = i − j + 1。

**例子**：时间（µs）为 `[0, 30, 60, 90, 150, 160]`。
- 第 4 个包（t = 150）：窗口是 (50, 150]，第一个落在窗口里的是 60，位置 j = 2。
- 包数 = 4 − 2 + 1 = **3**（60、90、150）。
- 全部算出来是 `[1, 2, 3, 4, 3, 3]`。

**为什么这样做**：一次处理所有包，复杂度 O(N log N)。用 Python 循环逐行数，77 万个包会很慢。

**记不住时的替代写法**：先把时间设成 DatetimeIndex，然后 `s.rolling("100us").count()`。我验证过，结果完全一样。可以直接说："I'd count messages in a trailing 100-microsecond window; with a sorted time index, pandas rolling with a time offset does that."

---

## 3. 题卡

每张卡的结构都一样：**故事 → 思维链 → 问 → 讲 → 分块表 → 决策表 → 错误结论 → 代码骨架 → 报 → 追问**。

---

### F3 · A/B feed：B 线升级有没有用？（⭐1）

**故事**：同一个 multicast feed 在 A、B 两条线上各收一份，抓了两天（约 77 万个包，两条线共 154 万行）。B 线的交换机在两天之间升级了。问：升级有没有用？

**思维链**：Audit → 发现第二天更忙（confounder）→ 同一个包配对 A/B → 按负载分层 → 找排队拐点 → 报 limitations（幸存者偏差、只有两天）

**问**
1. "Are sequence numbers contiguous and reset daily, so a gap means a lost packet?"
2. "Are both lines timestamped by the same synchronised NIC clock?"
3. "By 'helped', do we care about the median, the tail, or how often B arrives first?"

**讲**
> "I'll do four steps. First, audit duplicates, out-of-order packets and missing seqs, keeping the earliest copy of each packet. Second, p50 and p99 per line and day, plus a clock-drift check before trusting one-way latency. Third, the upgrade: a naive before/after first, but since load may differ between days, I'll use line A as a control and compare A and B **on the same packet**, stratified by load. Fourth, latency vs load to find where queueing starts. With about 770k packets I'll stay vectorised; the rolling load uses a sort plus binary search."

| Block | 做什么 | 方法和函数 | 图 |
|---|---|---|---|
| 查 | 重复、乱序、缺号；两条线都丢的包 | `duplicated`、`groupby.cummax`、排序后 `groupby.diff()-1` | — |
| 量 | 每条线 × 每天的 p50/p99；时钟漂移 | `groupby.agg(median, quantile .99)`；每分钟最小值做 `np.polyfit` 斜率 | log-log 的 1 − ECDF |
| 控 ① | naive 前后对比，再算繁忙程度 | `quantile(.99).unstack()`；`(load >= 100).mean()` | — |
| 控 ② | 同一个包配对：B − A | `merge(a, b, on=["date", "seq"])`；中位数和"B 先到的比例" | — |
| 控 ③ | 按负载分层 | `searchsorted` 算负载 → `pd.cut` 分桶 → `groupby.median().unstack()` | 柱状图：每个负载桶的 B − A |
| 诊 | 延迟 vs 负载，找拐点 | 每 10 一个负载桶 → 中位数曲线 → `(curve > idle + 3).idxmax()` | 折线，拐点处加阴影 |

**决策表**

| 决定 | 为什么 | 代价 / 注意 |
|---|---|---|
| 重复的包保留第一份 | 第一份就是最早收到的那份 | 前提是原始文件按到达顺序排列 |
| 用每分钟的最小延迟查漂移 | 最小值基本不含排队时间，剩下的变化就是时钟 | 不能完全排除网络本身的变化 |
| 负载用 A、B 两条线的并集算 | B 自己丢的包会让负载被低估 | 两条线都丢的包（32 个）仍然数不到 |
| 同一个包 inner join | 流量、交易所、我们的时钟都抵消掉了 | 丢掉的包被排除了，会有**幸存者偏差** |
| 拐点 = 比空闲时高 3 µs | 简单、可解释、可复现 | 阈值是人定的，不是严格的容量估计 |

**幸存者偏差：最值得被追问的一点**
- 配对只能比较 B **成功收到**的包，而 B 丢的恰恰是最忙时刻的包：这些包在 A 线上的中位延迟是 **253 µs**，全体包是 25 µs。
- 所以 B 的配对中位数看起来很漂亮，却反映不出 B 在繁忙时的丢包。
- 评价升级要**延迟和可靠性一起看**：B 第二天的丢包率是 0.21%，而且丢包成串出现。
- 另一个 limitation：如果 A 在两天之间自己也变了，B − A 的变化就不完全等于升级的效果。要用更多天、其他没升级的路径，或者可控的实验来验证。

**错误结论**
- ❌ "B 的 p99 从 70 升到 111 µs，升级让它变差了。" → ✅ 没动过的 A 也从 66 升到 124 µs；第二天的繁忙程度是第一天的 3 倍（1.7% → 5.1%）。
- ❌ "rx − exch 就是准确的单程延迟。" → ✅ 两条线的每分钟最小值都以约 1 µs/h 的速度漂移，说明是时钟问题；只有同一个包的 B − A 可信。
- ❌ "配对结果很好，所以 B 全面更好。" → ✅ 有幸存者偏差，见上。

**代码骨架**
```python
df = pd.concat([pd.read_parquet(f"feed_{l}.parquet").assign(line=l) for l in "AB"])
# 查
df["dup"] = df.duplicated(["line", "date", "seq"])                         # keep first = earliest
df["ooo"] = df.seq < df.groupby(["line", "date"]).seq.cummax()             # arrived late
df = df[~df.dup].assign(lat_us=lambda d: (d.rx_ns - d.exch_ns) / 1e3)
gap = df.sort_values("seq").groupby(["line", "date"]).seq.diff() - 1       # > 0 = missing seqs
# 量
df.groupby(["line", "date"]).lat_us.quantile([.5, .99]).unstack()
drift = df.groupby(["line", "date", df.exch_ns // 60e9]).lat_us.min()      # per-minute min -> slope
# 控: load (union of A and B), pair the same packet, stratify by load
u = df.drop_duplicates(["date", "seq"]).sort_values("exch_ns")
u["load"] = np.arange(len(u)) - np.searchsorted(u.exch_ns, u.exch_ns - 100_000, side="right") + 1
p = df[df.line == "A"].merge(df[df.line == "B"], on=["date", "seq"], suffixes=("_a", "_b"))
p = p.merge(u[["date", "seq", "load"]], on=["date", "seq"])
p["b_minus_a"] = (p.rx_ns_b - p.rx_ns_a) / 1e3
p["bucket"] = pd.cut(p.load, [0, 25, 50, 75, 100, 150, 1000], right=False)
p.groupby(["bucket", "date"]).b_minus_a.median().unstack().plot.bar()
# 诊: knee
dl = df.merge(u[["date", "seq", "load"]], on=["date", "seq"])
load_bin = dl.load.clip(upper=160) // 10 * 10                              # last bin = 160+
curve = dl.groupby(["line", "date", load_bin]).lat_us.median().unstack(["line", "date"])
knee = (curve > curve.iloc[0] + 3).idxmax()
```

**F3 必会的 8 个 pattern**（按重要性排）

| pattern | 作用 |
|---|---|
| `merge(..., on=keys)` 配对 | 同一个包比较 A 和 B（极高） |
| `np.searchsorted()` | 滑动窗口负载（极高；记不住就用 `rolling("100us")`） |
| `groupby().transform("size")` | 组的大小填回每一行（比如这个 seq 在几条线上出现） |
| `groupby().diff() - 1` | 缺号 |
| `groupby().cummax()` | 乱序 |
| `groupby().agg(...)` | 一次算多个统计量 |
| `groupby().unstack()` | 多层 index 转成列 |
| `pd.cut()` + `idxmax()` | 负载分桶、找拐点 |

**报**
> "Yes, the data supports that the line-B upgrade helped. The naive comparison was misleading: B's p99 rose from 70 to 111 microseconds, but untouched line A got slower too, because the second day had three times as many high-load messages. On the same packets, median B − A went from +2.5 to −1.2 microseconds, an improvement of about 3.7 microseconds, at every load level, and B now arrives first 95% of the time instead of 1%. However, both lines start queueing around 70 to 80 messages per 100 microseconds, B drops packets in bursts, and 32 seqs were lost on both lines and need recovery. I'd keep the upgrade, look into burst capacity and packet loss, and confirm over more days: two days is strong evidence, but not a controlled experiment."

**追问**
- *Why not just compare p99s?* 两天的负载不同。同一个包配对，流量和交易所的影响就抵消了。
- *Can you trust absolute latency?* 不能完全信：rx 和 exch 来自两个时钟，有漂移。B − A 是同一个时钟下的差值。

---

### F1 · Gateway 日志：平均处理时间是多少？有没有问题？（⭐2）

**故事**：4 台 gateway、15 分钟、约 27 万行多线程日志，里面有坏行、重复和乱序。交易台问：平均处理时间是多少，有没有出问题？

**思维链**：解析 + 审计 → 按 clid 配对成一单一行 → p50/p99 而不是 mean → 开盘时段单独看（confounder）→ 按时间诊断：gw-3 周期性卡顿、ARCX 无响应 → 报

**问**
1. "By processing time, do you mean inside our gateway (IN→OUT), or including the exchange round trip?"
2. "Are IN, OUT and ACK stamped by the same host clock?"
3. "Should the market-open burst count as normal, or be reported separately?"

**讲**
> "I'll parse every line with one anchored regex and count each kind of line that doesn't match. Then I'll pair events by client order id, not by file order, to get one row per order with internal latency, round-trip time and a status. Latency is skewed, so I'll report p50 and p99 per gateway, and I'll split out the open burst so busy and quiet periods aren't mixed. Finally I'll look at *when* the slow and unanswered orders happen."

| Block | 做什么 | 方法和函数 | 图 |
|---|---|---|---|
| 查 | 解析，统计每一种跳过的行 | 逐行 `re.match`；`drop_duplicates`；时间戳统一成整数 ns | — |
| 建 | 一笔单一行 | `pivot_table(index="clid", columns="ev", values="ts")`；`np.select` 打 status | — |
| 量 | 按 gateway × 消息类型算 p50/p99/mean | `groupby.quantile([.5, .99]).unstack()` | log-log 的 1 − ECDF |
| 控 | 开盘 burst 单独看 | 每秒单数 `value_counts`，超过 3 倍中位数算 burst；按 venue × (open/normal) 算 RTT | — |
| 诊 | gw-3 什么时候慢？谁不回？ | 周期折叠 `t % 10s`；`(t.diff() > 5s).cumsum()` 切 incident | 散点：周期内的 ms vs 延迟；无响应时间线 |
| 加分 | 200 GB/天怎么办 | 流式读、log 直方图、多进程 | — |

**决策表**

| 决定 | 为什么 | 代价 / 注意 |
|---|---|---|
| 一个两头锚定的 regex，坏行计数后丢掉 | 宁可丢一行，也不信半条记录 | 太严格：格式一变就大量失败，所以一定要打印跳过的计数 |
| 时间戳转成整数 ns | µs 和 ns 混在一起；float 会丢精度 | 只解析了时分秒，所以只适用于单日数据 |
| 按 clid 配对，不按文件顺序 | 有 1,325 行乱序 | pivot 补 NaN 时会把 int 变成 float，所以用 `Int64` |
| 报 p50 + p99，不报 mean | 分布右偏，mean 会被 1% 的慢单拉高 | p99 要足够多的样本，表里标上 n |
| 开盘 burst 单独看 | 负载高时 RTT 也高，混在一起比较不公平 | 3 倍这个阈值是人定的，要核对它确实是 14:30 开盘 |
| 按 10 s 周期折叠时间 | 看起来随机的尖峰变成一根柱子，说明是定时任务 | 周期要从数据里找（慢单间隔的众数），不能猜 |
| 无响应单独成一类 | 没有回复比慢更危险，订单状态未知 | 不能计入 RTT，否则又是幸存者偏差 |

**错误结论**
- ❌ "平均 37 µs，gw-3 整体都慢。" → ✅ 四台 gateway 的 p50 都是 8 µs；gw-3 只有约 1% 的单是毫秒级。
- ❌ "14:30 附近变慢是开盘负载造成的。" → ✅ 按周期折叠以后，15 分钟里每个 10 s 周期都有卡顿，所以不是开盘造成的。
- ❌ "ARCX 的 RTT 正常，所以 ARCX 没问题。" → ✅ 有 262 单根本没回复，不在 RTT 统计里。
- ❌ "全局 p99 = 4 台 gateway 的 p99 平均 = 343 µs。" → ✅ 合并直方图得到的是 18 µs。

**代码骨架**
```python
rows, skipped = [], {}
for line in open("gateway.log"):
    if m := EVENT_RE.match(line):                 # ts, gw, ev, mtype, clid, venue, reason
        rows.append(m.groups())
    else:
        skipped[kind(line)] = skipped.get(kind(line), 0) + 1   # HB / ERROR / traceback / truncated
ev = pd.DataFrame(rows, columns=cols).drop_duplicates()      # ts -> int ns
o = ev.pivot_table(index="clid", columns="ev", values="ts", aggfunc="min")   # one row per order
o["internal_us"] = (o.OUT - o.IN) / 1e3
o["rtt_us"] = (o.ACK.fillna(o.REJ) - o.OUT) / 1e3
o["status"] = np.select([o.ACK.notna(), o.REJ.notna(), o.OUT.isna()], ["acked", "rejected", "not sent"], "no response")
o.groupby(["gw", "mtype"]).internal_us.quantile([.5, .99]).unstack()
per_s = (o.OUT // 1e9).value_counts(); burst = per_s[per_s > 3 * per_s.median()]
phase_ms = (gw3.IN % 10e9) / 1e6                    # fold time by the 10 s cycle
nr["incident"] = (nr.t.diff() > 5e9).cumsum()       # gap-based clustering
```

**报**
> "A typical order spends 8 microseconds in our gateways, 5 for a cancel, the same on all four, so I'd report p50 and p99 per gateway, not one mean. gw-3 stalls for about 120 milliseconds every 10 seconds; outside that window its p99 is 17 microseconds like the others, which points to a periodic job on that host. ARCX stopped answering for 20 seconds from 14:36:41 on all four gateways, leaving 262 orders in an unknown state to reconcile, not resend, and gw-2 alone hit the rate limit at the open. Limitations: one 15-minute window, log time is when the line was written, and the stall's cause is inferred from timing, not proven."

**追问**
- *Can you average the 40 gateways' p99s?* 不能。每台机器发一个直方图，加起来再求 p99（18 µs，平均 p99 会得到 343 µs）。
- *An order got no answer. Resend it?* 不能：订单状态未知，重发可能重复成交。应该先对账（reconcile）。

---

### F4 · IOC hit rate：速度值多少？"S2 更好"对不对？（⭐3）

**故事**：17.7 万次 IOC 抢单尝试，5 个 venue、2 个策略、20 天。问：hit rate 稳不稳定？速度有多重要？"S2 的 hit rate 更高，所以 S2 更好"对不对？

**思维链**：Audit → 定义 hit rate 并带上 CI → 画每日图找断点，判断是我们变了还是市场变了 → 不跨断点合并 → race curve → 按 venue 拆开，发现 Simpson's paradox → 标准化 → 报

**问**
1. "Is a partial fill a hit, or do we care about the share of quantity filled?"
2. "Did anything change on our side during these 20 days, like a release?"
3. "Are attempts independent, or can one opportunity trigger several attempts?"

**讲**
> "First I'll audit and define the hit rate, with confidence intervals. Before pooling 20 days, I'll plot the daily rate per venue and flag jumps far outside noise, then check whether *our* latency moved at each break: us or them. Within a stable window, I'll bin attempts by our reaction time to get a race curve per venue. Finally, I'll test 'S2 is better' venue by venue, because the two strategies may trade different venues."

| Block | 做什么 | 方法和函数 | 图 |
|---|---|---|---|
| 查 | 重复行；缺失或 ≤ 0 的延迟 | `drop_duplicates`；`where(r > 0)` 置 NaN，保留整行 | — |
| 量 | 按 venue、策略算 hit rate 和 95% CI | `groupby.agg(n, k)` → p ± 1.96·√(p(1−p)/n) | — |
| 控 ① 时间 | 每日 hit rate，找断点 | `unstack` 成天 × venue；相邻两天做 z 检验，\|z\| > 4 | 折线加 CI 带（`fill_between`） |
| 诊 | 我们变了还是市场变了？ | 断点前后比较我们的中位延迟：`pd.cut(day)` 分期 | — |
| 控 ② 速度 | race curve | 第 1–15 天，`pd.qcut` 每个 venue 分 10 档，算每档的 hit rate | `errorbar` |
| 控 ③ 构成 | S1 vs S2 | 第 1–10 天；`pd.crosstab(normalize="columns")` 算 venue 构成 → 标准化 | 分组柱状图 |

**决策表**

| 决定 | 为什么 | 代价 / 注意 |
|---|---|---|
| 延迟坏了也保留整行，只把延迟置 NaN | 成交结果是真的，删行会让 hit rate 有偏 | 只在 race curve 里排除这些行 |
| hit = 有任何成交 | 回答的是"赢没赢 race" | 不反映成交量，所以另外给一个按数量算的比例 |
| 断点阈值 \|z\| > 4，不用 1.96 | 95 次检验，用 1.96 会有约 5 个假警报（multiple testing） | 小的真实变化可能被漏掉 |
| 不跨断点合并：race curve 只用第 1–15 天，S1 vs S2 只用第 1–10 天 | 不同 regime 混在一起没有意义 | 样本变少 |
| `qcut` 等样本量分档 | 尾部也有足够样本 | 档宽不等；延迟双峰时中间那段没数据，图上要断线 |
| 直接标准化：用 S1 的 venue 构成 | 在同样的机会下比较两个策略 | 用谁的构成是一个选择，要说清楚 |

**错误结论**
- ❌ "S2 的 hit rate 0.748 高于 S1 的 0.679，所以 S2 更好。" → ✅ S1 在每个 venue 上都更高。S2 有 85% 的尝试在最容易的 EX5；换成 S1 的构成，S2 只有 0.218。
- ❌ "EX3 第 16 天暴跌，是我们的问题。" → ✅ 我们的延迟没变，两个策略一起跌，所以是外部原因，多半是更快的竞争者。
- ❌ "把 20 天合并起来画 race curve。" → ✅ 会把 EX3 的新 regime 混进去。
- ❌ "race curve 的斜率就是速度的因果价值。" → ✅ 慢的那几档大多是 S2，混了策略和 edge。

**代码骨架**
```python
df = pd.read_parquet("take_attempts.parquet").drop_duplicates()
df["reaction_us"] = df.reaction_ns.where(df.reaction_ns > 0) / 1e3   # keep row, blank bad latency
df["hit"] = df.filled_qty > 0
df["day"] = df.date.rank(method="dense").astype(int)               # trading day 1..20
# 量 + CI
g = df.groupby("venue").hit.agg(k="sum", n="size"); p = g.k / g.n
ci = 1.96 * np.sqrt(p * (1 - p) / g.n)
# 控 ①: daily breaks
k = df.groupby(["day", "venue"]).hit.sum().unstack(); n = df.groupby(["day", "venue"]).hit.size().unstack()
rate, var = k / n, k / n * (1 - k / n) / n
z = rate.diff() / np.sqrt(var + var.shift())                       # |z| > 4 = break
# 控 ②: race curve, stable window only
d = df[(df.day < 16) & df.reaction_us.notna()].copy()
d["bin"] = d.groupby("venue").reaction_us.transform(lambda s: pd.qcut(s, 10, labels=False))
curve = d.groupby(["venue", "bin"]).agg(us=("reaction_us", "median"), hit=("hit", "mean"))
# 控 ③: Simpson + standardization, before the day-11 release
d = df[df.day < 11]
by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
same_mix = by_venue.mul(mix.S1, axis=0).sum()                       # both strategies on S1's mix
```

**报**
> "Three conclusions. First, hit rate is a latency race: on every venue it falls as we get slower, from 0.83 to 0.21 on EX1, the most contested venue. Second, there are two breaks: on day 11 our release made S1 faster, from 1.40 to 1.10 microseconds, and lifted EX1 and EX2; on day 16 EX3 fell from 0.76 to 0.35 for both strategies while our latency didn't move, so that's external, probably a faster competitor. I never pool across those days. Third, S2's higher headline hit rate is Simpson's paradox: 85% of its attempts are on EX5, the easiest venue. S1 wins on every venue, and on S1's venue mix S2 would hit only 22%. Limitations: this is observational, hit rate isn't P&L, and the curves expire when competitors change."

**追问**
- *What is 200 ns worth?* 把 race curve 当查找表：每次尝试的 hit rate 变化 × 数量 × edge，加总再按天平均。注意它是观察数据，不是因果。
- *Why not 1.96 for the breaks?* 95 次检验会有约 5 个假警报，所以阈值要提高。

---

### F2 · Order book：从 feed 重建 book，再描述市场（4，mock 用）

**故事**：一只股票 30 分钟的 L2 JSON feed，有坏行、重复、乱序和丢包。要求：正确重建 BBO，再描述市场（spread、成交方向、imbalance 能不能预测下一跳）。

**思维链**：按到达顺序审计 → 去重，按 seq 排序后 replay，缺号就标 stale → 和 truth 核对，再用 naive replay 做对照组 → spread 分按事件和按时间两种加权 → quote rule 判断成交方向 → imbalance 分档，样本外检验 → 实时模式用 reorder buffer → 报

**问**
1. "Is `qty` in an update the new total at that price, or a delta?"
2. "A snapshot with seq S: is that the book before or after message S?"
3. "Offline analysis of the whole file, or should I design it like a live handler?"

**讲**
> "I'll audit the feed in arrival order first: bad lines, duplicates, late seqs, gaps and price precision. Since I have the whole file, I'll dedupe and sort by seq, replay into two dicts of tick to size, and mark the book stale from any gap until the next snapshot. I'll check it against the truth file, and against a naive arrival-order replay as a control. Then I'll describe the market: spread per update vs per unit time, trade direction with the quote rule, and whether imbalance predicts the next move, tested out of sample."

| Block | 做什么 | 方法和函数 | 图 |
|---|---|---|---|
| 查 | 坏行、重复、晚到、缺号、价格噪声 | 逐行 `json.loads` 加 try；在**到达顺序**上做 `cummax`；`set(range) - set(seq)` 找缺号 | — |
| 建 | 重建 book | dict 去重、按 seq 排序 → replay（snapshot 重置、qty 是绝对值、0 表示删档）→ 缺号标 stale | 每秒消息数，stale 时段涂红 |
| 控 | 证明它是对的 | 和 truth 核对 + `assert bid < ask`；naive 按到达顺序 replay 当对照组 | — |
| 量 | spread、成交方向 | 按事件 vs 按时间加权（权重 = 这个状态持续多久）；quote rule：`sign(2·px − bid − ask)` | step 图加成交点；离散柱状图 |
| 诊 | imbalance 信号 | 前半段 `quantile` 定分档边界 → `pd.cut` → 每档 P(下一跳上涨) | 折线：样本内 vs 样本外 |
| 加分 | 实时模式 | heap 做 reorder buffer，扫等待预算 | 表格 |

**决策表**

| 决定 | 为什么 | 代价 / 注意 |
|---|---|---|
| 价格转 tick 用 `round(px*100)` | `int()` 是截断，有 4,351 个价格会差一个 tick | — |
| 审计在到达顺序上做 | 先排序的话，乱序的证据就没了 | — |
| 离线时去重后按 seq 排序 | 最简单、最不容易错；不按 ts 排，因为很多消息 ts 相同 | 只适用于离线；实盘要 reorder buffer |
| book = 每边一个 dict，best 用 `max`/`min` | 约 50 档，O(档数) 足够 | 档位多或要求低延迟时，换 cached best 或 tick 数组 |
| 缺号标 stale，等下一个 snapshot | 宁可说"现在不可信"，也不给错误的 book | 损失 28 s 的可用时间，能重传就请求重传 |
| spread 两种加权都报 | 回答的问题不同；按事件加权会偏向忙的时段 | 要说明用的是哪种口径 |
| imbalance 分档边界只用前半段定 | 避免 look-ahead | 样本少一半 |
| reorder buffer 等 50 ms | 用数据扫出来的拐点：0 ms 时 book 1,592 s 都是 stale | 遇到真 gap，后面的消息要多等 |

**错误结论**
- ❌ "按到达顺序直接 apply 就够了。" → ✅ 有 1.86% 的行是错的，还出现 2,461 次 crossed book：晚到的旧 update 把已经删掉的档位"复活"了。
- ❌ "平均 spread 是 1.63 tick。" → ✅ 那是按事件算的；按时间加权是 1.32 tick，1 tick 占 77% 的时间。
- ❌ "imbalance 预测 66%，所以能赚钱。" → ✅ 方向不等于 P&L：还要扣 spread、latency 和排队位置。

**代码骨架**
```python
msgs, bad = [], 0
for line in open("feed.jsonl"):
    try: msgs.append(json.loads(line))
    except json.JSONDecodeError: bad += 1
tick = lambda px: round(px * 100)
ordered = sorted({m["seq"]: m for m in msgs if "seq" in m}.values(), key=lambda m: m["seq"])
book, prev, stale, rows = {"B": {}, "A": {}}, 0, False, []
for m in ordered:                                       # replay
    if m["type"] == "snapshot": book = {...}            # full reset
    elif m["type"] == "update":
        side = book[m["side"]]
        if m["qty"]: side[tick(m["px"])] = m["qty"]
        else: side.pop(tick(m["px"]), None)
    stale = m["type"] != "snapshot" and (stale or m["seq"] != prev + 1)
    prev = m["seq"]; rows.append((m["seq"], m["ts"], max(book["B"]), min(book["A"]), stale))
bbo = pd.DataFrame(rows, columns=["seq", "ts", "bid", "ask", "stale"])
# spread: per update vs per unit time
live = bbo[~bbo.stale].assign(spread=lambda d: d.ask - d.bid)
live["dur"] = live.ts.diff().shift(-1); live.spread.mean(); np.average(live.spread, weights=live.dur)
```

**报**
> "I audited the feed first: one bad line, 215 duplicates, 857 late arrivals and a three-message gap. Deduping and sorting by seq, with the book marked stale from the gap to the next snapshot, matches the exchange's BBO on 100% of live rows, while a naive arrival-order replay is wrong on 1.86% of rows and even crosses. The spread is 1.32 ticks per unit time against 1.63 per update, because wide spreads come in busy bursts. Imbalance calls the next mid move 66% of the time out of sample. Live, a 50-millisecond reorder buffer gives the same book; the limits are that a real gap still costs 28 seconds without retransmission, and that direction isn't P&L."

**追问**
- *Multi-core, 5,000 symbols?* 按 symbol 分 shard，每个 book 只有一个写线程，用 SPSC ring 喂消息，热路径不加锁。
- *Order-by-order add/cancel/modify?* `order_id → 节点` 的 hash map，加每个价位一个 FIFO 链表，都是 O(1)。改小数量保留排队位置；改价或加量 = cancel + new。

---

### F5 · TCP 40 ms：为什么有些单要 40 ms？（5）

**故事**：一次发版之后，部分订单的 ack 要约 40 ms，正常应该是约 30 µs。4 个 TCP session，一小时数据。找原因、拿数据证明、估算影响。

**思维链**：Audit（时间戳因果顺序）→ 把延迟拆成两段：我们主机（app→wire）和网络（wire→ack）→ 看分布：双峰，只有 s3 慢 → 按配置拆表，s4 当对照组 → 验证机制：被卡住的那部分在下一个入站包到达时放行，否则等到 40 ms 定时器 → 估算修复后的 p99 → 报

**问**
1. "Are app, NIC-TX and NIC-RX timestamps from PTP-synced clocks?"
2. "What changed in this morning's release, and on which sessions?"
3. "Is the 40 ms on every message type, or only some?"

**讲**
> "First I'll check timestamps are causally ordered, then split each order's latency into our host (app to wire) and network plus exchange (wire to ack), so I know where the time goes. I'll compare the distributions per session with ECDFs, because the healthy case is 30 microseconds and the bad one 40 milliseconds; a mean would be meaningless. Then I'll break slow orders down by configuration to find the one difference that matters, using a session that differs in only that setting as a control. Finally, I'll test the mechanism directly in the timestamps."

| Block | 做什么 | 方法和函数 | 图 |
|---|---|---|---|
| 查 | 因果顺序、空值 | `Int64` 读 ns（float 存不下精确的 ns）；`assert` 时间先后顺序 | — |
| 建 | 拆阶段 | app→wire、wire→ack、total；hold = 最后一个字节 − 第一个字节 | — |
| 量 | 按 session 看分布 | `groupby.quantile([.5, .99])`；超过 1 ms 的比例 | ECDF（log-x）：total 和 network 两个面板 |
| 控 | 找唯一差异 | `groupby([session, version, kernel, rack, nodelay, write_calls])` 算慢单比例；s4 当对照组 | — |
| 诊 | 验证机制 | `pd.merge_asof(direction="forward", by="session")` 找下一个入站的 FILL/HEARTBEAT 包 | 散点：hold vs 到下一个入站包的时间 |
| 报 | 估算影响 | 修复后的 p99 ≈ 不含 s3 的 p99 | — |

**决策表**

| 决定 | 为什么 | 代价 / 注意 |
|---|---|---|
| 先拆阶段 | 先定位时间花在哪一段：网络那段每个 session 都一样，所以问题在我们的主机上 | 需要可信的 NIC 时间戳 |
| 用分位数和 log-x ECDF，不用 mean | 30 µs 和 40 ms 差 1,000 倍 | — |
| s4 当对照组：同 build、同 kernel、同机架，只有 nodelay 不同 | 一次只变一个变量；s4 慢单 0% | 是自然实验，不是随机实验 |
| 入站包只用 FILL/HEARTBEAT，不用 ACK | 被卡订单自己的 ACK 一定在放行之后，用它就是循环论证 | — |
| 用 `merge_asof` 找下一个入站包 | 直接检验"hold 时间 = 到下一个入站包的时间" | 假设对方的 delayed-ACK 定时器是 40 ms |
| 修复后 = 去掉 s3 的分布 | 修好之后 s3 应该和 s4 一样 | 假设其他条件不变 |

**错误结论**
- ❌ "网络慢了。" → ✅ 每个 session 的 wire→ack 中位数都在 27–29 µs，时间全花在我们主机上。
- ❌ "是新 kernel 或机架 R12 的问题。" → ✅ s2（不同 kernel）和 s1（不同机架）都是 0% 慢单，是 red herring。
- ❌ "只有发 2 次 send() 的单慢。" → ✅ s3 里只发 1 次 send() 的单也有 41.9% 慢：它们排在被卡住的单后面，一起被放行。

**代码骨架**
```python
o = pd.read_csv("orders.csv", dtype={"exch_ack_ns": "Int64"})        # nullable int: keep ns exact
o["app_to_wire_us"] = (o.wire_last_ns - o.app_send_ns) / 1e3           # our host
o["wire_to_ack_us"] = (o.exch_ack_ns - o.wire_last_ns) / 1e3           # network + exchange
o["total_us"] = (o.exch_ack_ns - o.app_send_ns) / 1e3
o.groupby("session")[["app_to_wire_us", "wire_to_ack_us"]].quantile([.5, .99]).unstack()
for s, g in o.groupby("session"):                                       # ECDF on log-x
    x = np.sort(g.total_us.dropna()); ax.plot(x, np.arange(1, len(x) + 1) / len(x), label=s)
df = o.merge(sessions, on="session")
df["slow"] = df.app_to_wire_us > 1000
df.groupby(["session", "app_version", "tcp_nodelay", "write_calls"]).agg(pct_slow=("slow", "mean"))
held = pd.merge_asof(held.sort_values("probe_ns"), inbound_no_ack.sort_values("rx_ns"),
                     left_on="probe_ns", right_on="rx_ns", by="session", direction="forward")
ax.scatter(held.to_next_ms, held.hold_ms, s=4, alpha=.3)                # diagonal, then flat at 40 ms
```

**报**
> "10.5% of orders, all on session s3, take about 40 milliseconds instead of 30 microseconds. The network leg is identical on every session, so the time is lost on our host. The new build sends NewOrder in two send() calls, and s3 has TCP_NODELAY off, so Nagle holds the second part until the exchange's delayed ACK, up to 40 ms. Session s4 runs the same build, kernel and rack with NODELAY on and has no slow orders. In the timestamps, held messages leave either right after the next inbound packet or at the 40 ms timer. Setting TCP_NODELAY and using one send() per message should take p99 from 40.6 ms to about 50 microseconds; I'd verify with a canary on s3 against s4. Limitations: the mechanism is inferred from timestamps, not seen in a packet capture, and the 40 ms timer is assumed."

**追问**
- *Why does the body leave at the next inbound packet?* 对方的 ACK 可以搭在它发给我们的数据包上一起回来；ACK 一到，Nagle 就放行。
- *How do you prove the fix?* 只在 s3 上做 canary，s4 当对照，看 p99、超过 1 ms 的比例，再抓 pcap 确认。
