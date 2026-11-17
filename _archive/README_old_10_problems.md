# IMC QPE Onsite：Data Analysis / Algorithm Design 练习题集

> **最终练习用 [final5/](final5/STUDY_GUIDE.md)**：从下面 10 题中精选了 5 道，代码精简过，每道题配有一步一步的 guide。下面这 10 道题保留作扩展参考。

官方描述："a data analysis/algorithm design task conducted in Python … Pandas and a graphing library (matplotlib) may be helpful … the intent is to assess how you approach problems."

这里有 10 道完整的模拟题，每道都是一个 45–75 分钟的完整任务：有业务背景、带"脏数据"的数据集、逐级加深的 Part A→F、面试官可能追问的问题。每道题的数据里都**预设了几个需要你自己发现的 finding**，这和真实面试最像：面试官往往心里有答案，在看你能不能一步步找到它。

## 快速开始

```bash
cd data_practice
```

```bash
python3 generate_all.py
```

生成全部数据（约 1–2 分钟，写到 `data/` 下）。只需要 numpy / pandas / matplotlib / scipy / pyarrow，**不需要** sklearn 或 pytest，模拟面试环境里只有基础库的情况。

单独生成某一题的数据：`python3 generate_all.py P03`，或者进到题目目录运行 `python3 make_data.py`。数据总共约 270 MB，其中大头是 P02 的 3M 行 CSV。P07 重新生成数据后，先跑一次 `python3 solution.py --bless` 生成 golden file，regression test 才会执行。

每个题目目录里有这些文件：

| 文件 | 用途 |
|---|---|
| `problem.md` | **面试题面（英文）**。先只看这个，计时开始做 |
| `make_data.py` | 生成数据（固定 seed，可复现） |
| `solution.py` | 参考解答，`python3 solution.py` 会打印结果，图存到 `plots/` |
| `answers.md` | **中文讲解**：预设的 findings、追问的参考答案、常见扣分点、英文 speak-aloud 话术 |
| `test_solution.py` | （P02 / P07 / P08）unittest，演示 unit / e2e / regression test 怎么写 |

## 题目一览

| # | 题目 | 场景 | 覆盖的关键词 |
|---|---|---|---|
| P01 | [Gateway Log Latency](P01_gateway_log_latency/problem.md) | 解析 gateway 文本日志，算 order processing latency | parse text data、average/percentile processing time、regex、generator streaming、p99 vs mean、周期性异常、log 乱序/重复/截断 |
| P02 | [CSV Average: Performance Deep-Dive](P02_csv_average_perf/problem.md) | 网上那道"读 CSV、按股票求平均"再加 6 个追问 | data structures、memory mgmt & error handling、unit/e2e test、latency/throughput/handoff latency、cProfile/tracemalloc、multiprocessing、pandas vectorization、sampling vs tracing profiler |
| P03 | [Order Book from JSON Stream](P03_orderbook_json_stats/problem.md) | 从 JSON 增量流重建 L2 order book，算微观结构统计并可视化 | json stream、orderbook 展示、seq gap/dup/乱序、snapshot recovery、spread/imbalance/VWAP、time-weighted stats、depth heatmap |
| P04 | [A/B Feed Latency & Shift](P04_ab_feed_latency_shift/problem.md) | 交易所 A/B 两路 multicast feed 的延迟研究，判断交换机升级是否有效 | microstructure latency research、parquet、arbitration、packet loss、clock drift、**distribution shift**、KS test、bootstrap、confounding、microburst |
| P05 | [IOC Hit Rate vs Latency](P05_ioc_hit_rate/problem.md) | 我们的 take 单抢单成功率，以及 latency 值多少钱 | hit rates、Wilson CI、Simpson's paradox、logistic regression、**distribution fitting**（竞争者延迟分布）、diff-in-diff |
| P06 | [Fill Model: Eval & Shift](P06_fill_model_eval_shift/problem.md) | 用 P05 的数据建 fill probability 模型，做评估和漂移检测 | **eval data**、leakage、time split、AUC/log loss/calibration、PSI、adversarial validation、concept drift |
| P07 | [Robust Ingestion Pipeline](P07_robust_pipeline/problem.md) | 从不可靠的远程存储加载分区数据，做缓存、修正和汇总 | loading performance、error recovery、retry/backoff、asyncio、partition、persistent cache、context manager、generator、pipeline corrections、unit/integration/regression test、Postgres/Mongo/Kafka 设计 |
| P08 | [Streaming Algorithms](P08_streaming_algorithms/problem.md) | 多个 port 的行情流合并、microburst、限流、滚动分位数 | algorithm design、k-way merge、watermark（Flink 思想）、two pointers、rate limiter、Fenwick tree、heavy hitters、复杂度分析 |
| P09 | [TCP 40ms Mystery](P09_tcp_40ms_mystery/problem.md) | 为什么部分订单延迟 40ms？然后是 networking 基础问答 | TCP/Nagle/delayed ACK、TCP_NODELAY、UDP multicast、kernel bypass、PTP、hardware timestamp |
| P10 | [Anomaly Detection on Monitoring](P10_anomaly_detection_monitoring/problem.md) | 服务器监控指标的异常检测和告警设计 | **anomaly detection**、seasonality、robust z/MAD、event-level precision/recall、CUSUM change point、heavy-tail fitting、QQ plot |

## 建议的练习顺序（按"最可能考"排序）

1. **P01 → P03 → P04**：最接近"parse/load 数据 → 分析 → 画图 → 讲结论"的官方描述，也最贴 QPE 的日常（latency 和 microstructure）。
2. **P02**：一定会被追问的 performance engineering 话题（profiling、benchmark、并发），把 6 个问题的答案练到能脱口而出。
3. **P05 → P06**：hit rate 和 ML/eval/shift，对应你搜到的 "OCC Hit Rates" 方向。
4. **P10 → P07**：anomaly detection、pipeline 鲁棒性、缓存。
5. **P08 → P09**：纯算法设计和网络基础，作为面试前一天的快速复习。

## 模拟面试的方法

- **计时**：只开 `problem.md`，Part A–D 控制在 45 分钟内，剩下时间做 stretch。
- **出声思考（in English）**：每一步先说 *what / why*，再写代码。面试官看的是你"怎么想"。
- **固定的分析流程**，每题都按这个走：
  1. **Clarify**：数据的单位、时区、时间戳是谁打的、什么叫"一笔"？
  2. **Sanity check**：`df.shape`、`dtypes`、`isna().sum()`、重复、乱序、范围、`describe()`。
  3. **Baseline 先跑通**：先用最简单的方法得出一个数字，再去优化。
  4. **看分布，不只看均值**：p50/p90/p99/p99.9，log-scale histogram 或 ECDF。
  5. **切片找原因**：groupby 不同维度（server / venue / time-of-day），画时间序列。
  6. **检查 confounding**：A 比 B 好，是不是因为两者的 mix 不同？
  7. **验证**：out-of-sample、和 ground truth 对比、写一个小测试。
  8. **一句话结论 + 一张图 + 下一步**：讲给 trader 或 network engineer 听。
- 做完再看 `answers.md`，对照"预设 findings"，检查自己漏了哪些。

## 面试用 pandas / matplotlib 速查（只列最常用的）

```python
# 读取
pd.read_csv(p, usecols=[...], dtype={"sym": "category"}, engine="pyarrow")
pd.read_parquet(p, columns=[...])
pd.read_json(p, lines=True)   # 坏行多时改用 for line in open(p) + try json.loads

# 时间
pd.to_datetime(s, utc=True, format="ISO8601")
s.dt.tz_convert("America/New_York")
(df.t1 - df.t0).dt.total_seconds() * 1e6   # 换算成 µs
# 纳秒整数用 int64 运算，不要转 float（精度会丢）

# 聚合
df.groupby("k").agg(n=("x", "size"), p99=("x", lambda s: s.quantile(.99)))
df.pivot_table(index=..., columns=..., values=..., aggfunc=...)
s.quantile([.5, .9, .99, .999])
pd.qcut(x, 10)   # 等频分箱
pd.cut(x, bins)  # 等宽分箱

# 时间序列
df.set_index("ts").resample("1s").agg(...)
s.rolling("100ms").count()          # 时间窗口滚动，需要 DatetimeIndex
pd.merge_asof(left, right, on="ts", by="sym", direction="backward")  # 行情和成交对齐，最常用

# 事件流
df.sort_values(["k", "ts"])
g.shift()  # 上一个事件
g.diff()   # 间隔
(cond != cond.shift()).cumsum()  # 把连续段编号

# 性能
# 用向量化（numpy / Series 运算），避免 iterrows / apply；字符串列转 category；
# 大文件用 chunksize 或 pyarrow；不要在循环里逐行 concat

# 画图（matplotlib）
fig, ax = plt.subplots(1, 2, figsize=(12, 4))
ax.hist(x, bins=np.logspace(...)); ax.set_xscale("log")   # latency 必用 log
ax.plot(np.sort(x), np.arange(1, len(x) + 1) / len(x))    # ECDF
ax.step(t, bid, where="post")                              # order book 价格用 step
```

## 推荐的准备网站与资料

**IMC 官方与相关岗位描述**（了解他们用什么工具、看重什么）
- [IMC Performance Engineer job posting](https://www.imc.com/us/careers/jobs/4547961101)：提到 "analysing terabytes of network data" 来找 pattern、验证 hypothesis。
- [IMC Trading Analytics Engineer posting](https://www.imc.com/ap/careers/jobs/4636511101)：明确写了 pandas / polars / numpy / scipy，强调 "optimization of performance on large data analytics"。
- [IMC Prosperity](https://prosperity.imc.com/)：IMC 自己办的 Python 算法交易比赛，有 order book 数据和接口文档（[Writing an Algorithm in Python](https://imc-prosperity.notion.site/Writing-an-Algorithm-in-Python-17be8453a09381988c6ed45b1b597049)）。社区有 backtester，比如 [prosperity4btest](https://pypi.org/project/prosperity4btest/)，可以拿来练 order book 数据分析。
- [Glassdoor 上的一条 IMC 面经](https://www.glassdoor.ie/Interview/IMC-Trading-Interview-E278100-RVW74382059.htm)（Data Engineer，Zürich）：HackerRank 考了 Pandas DataFrame、SQL 和 coding。只有一条，仅供参考。

**Pandas / 数据分析**
- [pandas: Enhancing performance](https://pandas.pydata.org/docs/user_guide/enhancingperf.html) 和 [Scaling to large datasets](https://pandas.pydata.org/docs/user_guide/scale.html)：vectorization、chunking、dtype 优化，对应可能的 efficiency 追问。
- [Modern Pandas (Tom Augspurger)](https://tomaugspurger.net/posts/modern-1-intro/)：method chaining、performance、time series，写法地道。
- [Python Data Science Handbook (Jake VanderPlas，免费)](https://jakevdp.github.io/PythonDataScienceHandbook/)：pandas 和 matplotlib 基础。
- [Matplotlib cheatsheets](https://matplotlib.org/cheatsheets/)
- [LeetCode 30 Days of Pandas](https://leetcode.com/studyplan/30-days-of-pandas/)、[DataLemur](https://datalemur.com/)、[StrataScratch](https://www.stratascratch.com/)：pandas 和 SQL 的短题，适合手速练习。

**真实的 order book / 市场数据（练 EDA）**
- [Kaggle: Optiver Realized Volatility Prediction](https://www.kaggle.com/competitions/optiver-realized-volatility-prediction) 和 [Optiver Trading at the Close](https://www.kaggle.com/competitions/optiver-trading-at-the-close)：做市商出的题，有真实 book 数据，公开 notebook 里有很多 EDA 可以参考。
- [LOBSTER sample data](https://lobsterdata.com/info/DataSamples.php)：免费的 NASDAQ L3 消息与 order book 样本。
- [Databento microstructure guide](https://databento.com/microstructure)：MBO/MBP、sequence number、snapshot 等术语讲得清楚。

**Performance engineering / latency 测量**
- Python 官方文档：[profile/cProfile](https://docs.python.org/3/library/profile.html)、[tracemalloc](https://docs.python.org/3/library/tracemalloc.html)、[asyncio](https://docs.python.org/3/library/asyncio.html)
- Profiler 工具：[py-spy](https://github.com/benfred/py-spy)（sampling）、[Scalene](https://github.com/plasma-umass/scalene)（CPU + memory）、[memray](https://github.com/bloomberg/memray)（allocation tracing）
- [Brendan Gregg 的网站](https://www.brendangregg.com/)：flame graph、[USE method](https://www.brendangregg.com/usemethod.html)、perf。Performance engineer 必读。
- [HdrHistogram](http://hdrhistogram.org/)，以及 Gil Tene 的演讲 *"How NOT to Measure Latency"*（YouTube 搜标题）：coordinated omission、为什么分位数不能平均。
- [Latency numbers every programmer should know](https://gist.github.com/jboner/2841832)
- [Mechanical Sympathy blog](https://mechanical-sympathy.blogspot.com/)：低延迟系统的经典博客。
- [AWS: Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)：P07 的 retry 设计出自这里。

**Networking**
- [High Performance Browser Networking（免费在线）](https://hpbn.co/)：前几章 [TCP building blocks](https://hpbn.co/building-blocks-of-tcp/)、UDP 讲得最清楚，包括 slow start、Nagle、head-of-line blocking。
- [Beej's Guide to Network Programming](https://beej.us/guide/bgnet/)：socket 编程基础。
- [Julia Evans / wizardzines](https://wizardzines.com/)：tcpdump、网络调试的直观小漫画。

**书**（时间够的话挑着读）
- *High Performance Python*（Gorelick & Ozsvald）：profiling、numpy、并发，和 P02 一一对应。
- *Designing Data-Intensive Applications*（Kleppmann）：partition、replication、stream processing，对应 Kafka/Flink 的追问。
- *Trading and Exchanges*（Larry Harris）：市场微观结构的入门经典。

## 和之前材料的关系

`prep_code/` 里是 **system design 和 OOP** 部分（order book matching、market data recovery、OMS FSM、max flow、knapsack）。这里补的是 **technical interview（data analysis / algorithm design）** 部分。P03 和 P08 会用到 `prep_code/02_market_data_recovery` 里的 seq gap 和 snapshot 思想，可以对照看。
