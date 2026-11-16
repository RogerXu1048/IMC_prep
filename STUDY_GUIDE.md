# Final 5：总 Study Guide

> 先读这份通用指南，再按顺序做每道题文件夹里的 `guide.md`。
> 目标：每道题能在 2 小时内从空文件写到结论，边写边用英文讲清楚**为什么这么做**。

## 1. 为什么是这 5 道

每道题都满足四个条件：
- 和你搜到的 topic 直接相关；
- 同时包含数据分析和一个算法点；
- 用到 pandas 和 matplotlib；
- 2 小时能写完。

| # | 题目 | Topic | 数据分析部分 | 算法部分 | 图 |
|---|---|---|---|---|---|
| F1 | [Order latency from gateway logs](F1_order_latency_from_logs/problem.md) | 解析文本日志求平均处理时间；microstructure latency | 分位数表、按 gateway/venue 切片、找周期性异常 | generator 流式解析；用 hash map 配对事件，O(n)；可合并的 histogram | ECDF + 慢单 vs 时间 |
| F2 | [Order book rebuild from a JSON feed](F2_orderbook_rebuild/problem.md) | Research microstructure | spread（事件加权 vs 时间加权）、主动方判断、imbalance 的预测力 | L2 book 数据结构；sequencer（去重、乱序缓冲、缺号后 stale 直到 snapshot） | 盘口 step 图 + imbalance 分组 |
| F3 | [A/B feed latency & a network change](F3_ab_feed_latency/problem.md) | Market microstructure latency research | 分位数、时钟漂移、upgrade 前后对比、配对比较、按负载分桶 | A/B arbitration（按 seq 合并、取先到）；用 `searchsorted` 做双指针算滑窗消息率 | 延迟分布 + 延迟 vs 负载 |
| F4 | [IOC hit rate vs latency](F4_ioc_hit_rate/problem.md) | OCC hit rates；US equity options | 命中率与置信区间、结构性断点、Simpson's paradox | Wilson CI；直接标准化；race curve 插值估算"快 200ns 值多少钱" | 每日命中率 + race curve |
| F6 | [Quote pick-offs & markouts](F6_quote_pickoffs/problem.md) | **quoting** strategies（做市一侧）；US equity options；latency research | markout 曲线、stale fill 的占比与亏损、requote latency 拆解、race curve | 多键 as-of join（`merge_asof(by=…)`）；用排序不等式证明"按 \|delta\|×size 排队"最优；counterfactual 估算 | markout 曲线 + latency vs 队列位置 / race curve |

**建议顺序**：F1 → F3 → F2 → F4 → F6。
- F1 最基础，是"读数据、配对、统计"的模板。
- F3 和 F2 难度更高，算法分量更重。
- F4 和 F6 是同一场比赛的两面：F4 我们是 taker，F6 我们是 maker。连着做，race curve 的思想会很熟。

**和 JD 的对应**：
- exchange technology：F2、F3
- connectivity providers：F3
- our own systems：F1、F6
- liquidity taking：F4
- quoting：F6
- reverse-engineering：F3、F6，其中 F6 是从时间戳反推出引擎在串行发送 requote
- 和 traders / engineers 沟通：每题收尾都有

网络基础原来是单独一题（F5，TCP 40ms），现在移到了 [extra_F5_tcp_40ms](extra_F5_tcp_40ms/guide.md)，只读 guide、练口头问答即可。

准备数据：

```bash
python3 generate_all.py
```

## 2. 面试到底在看什么

官方原话："the intent of the interview is to assess how you approach problems, not familiarity with Python or these libraries. Interviewers will be happy to assist with syntax."

所以面试官主要在打分这四件事：
1. **Approach**：先澄清，再看数据，再给出 baseline，然后迭代。不要一上来就写复杂代码。
2. **Decisions**：每个选择都能说出理由，比如为什么用 p99 而不只用 mean、为什么按 seq 而不是 timestamp 排序。
3. **Trade-offs**：知道另一种做法是什么、代价是什么、什么条件下你会换过去。
4. **Communication**：结论讲给 trader 或 network engineer 听，一句话、一张图、一个建议。

语法不熟没关系，直接问。但**不要沉默地卡住**。

## 3. 通用 2 小时节奏

| 时间 | 做什么 | 要说的话（例句） |
|---|---|---|
| 0–5 min | 澄清问题、说出假设 | "Before I start, a few questions: what are the units of the timestamps, and who stamps them? Is the file small enough to fit in memory, or should I assume it's streamed?" |
| 5–20 min | 加载、做数据审计（shape、dtypes、缺失、重复、乱序、范围） | "I'll first audit the data, because every later number depends on it. I'll count bad rows rather than silently dropping them." |
| 20–40 min | 跑通最简单的 baseline，先得到第一个数字 | "Let me get a simple end-to-end answer first, then refine it." |
| 40–90 min | 核心分析和算法：切片、找原因、正确比较 | "The average hides the tail, so I'll look at p99 by gateway." |
| 90–105 min | 1–2 张图，用来证明结论 | "One plot that makes the root cause obvious." |
| 105–120 min | 结论、局限、下一步 | "Three findings, the evidence for each, and what I'd do next." |

**时间不够时的优先级**：给出一个正确的数字，比一个没写完的完美方案重要。每完成一步就 print 结果，确保手上永远有一个能讲的结论。

## 4. 每一步都走的 approach 循环

1. **Clarify**：输入是什么、输出是什么、成功标准是什么。
2. **Inspect**：先看 5 行原始数据，再做 `df.info()`、`describe()`、`isna().sum()`、`duplicated().sum()`。
3. **Baseline**：用最笨但正确的方法先得到一个数。
4. **Check**：这个数合理吗？和量级常识、和 ground truth、和另一种算法对比一下。
5. **Refine**：切片、控制混杂因素、处理边界情况。
6. **Visualize**：只画能支撑结论的图。
7. **Explain**：说清结论、证据、局限和下一步。

## 5. 怎么讲 decision 和 trade-off

固定句式（多练几遍，变成条件反射）：

> "I'm choosing **X** over **Y** because **[reason tied to this data/goal]**. The cost is **[what X gives up]**. If **[condition]** were true, I'd switch to **Y**."

例：
> "I'm pairing events with a dict keyed by order id rather than a pandas pivot, because it streams in one pass with memory proportional to the number of open orders. The cost is more code than a one-line pivot. If the file fits in memory, which it does here, the pivot is simpler, so I'll use the pivot and mention the streaming version for production."

这 5 道题里反复出现的 trade-off：

| 维度 | 选项 A | 选项 B | 怎么选 |
|---|---|---|---|
| 统计量 | mean | p50/p99/p99.9 | latency 是重尾分布，一定要报分位数；mean 用于算容量和总成本 |
| 顺序 | arrival order / timestamp | sequence number | 交易所的状态以 seq 为准；timestamp 可能来自不同时钟、可能乱序 |
| 内存 | 全部 load 进来 | 流式处理（generator） | 面试数据小就全 load，但要说出生产环境怎么流式 |
| 精确 vs 近似 | 精确分位数（排序） | histogram 或 sketch（可合并） | 单机用精确值；跨机器、跨天要合并时用 histogram，**不能平均 p99** |
| 坏数据 | 丢弃 | 隔离并计数（quarantine） | 永远计数并报告，不要静默丢掉 |
| 比较两组 | 直接比 | 配对比较 / 分层 / 标准化 | 两组构成不同时（流量、venue mix），直接比会骗人 |
| 价格 | float | 整数 tick | 账本和 book 一律用整数，避免浮点误差 |
| 实现 | pandas 向量化 | Python 循环 | 有状态的算法（book、sequencer）用循环更清楚；聚合和统计用 pandas |
| 阈值 | 固定 | 自适应 / 分桶 | 有季节性或负载依赖时，阈值要分条件设定 |

## 6. "当作真的交易程序写"：代码骨架

每道题都从这个骨架开始。前 3 分钟写好它，后面往里填：

```python
"""F?: <one-line purpose>."""
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = Path(__file__).resolve().parent.parent / "data" / "pXX"
PLOTS = Path(__file__).resolve().parent.parent / "plots"
PCTS = [0.5, 0.9, 0.99, 0.999]          # DECISION: tails matter more than the mean


def step1_load_and_audit() -> pd.DataFrame:
    ...                                   # count every kind of bad row; never drop silently


def step2_core(df: pd.DataFrame) -> pd.DataFrame:
    ...


def main() -> None:
    df = step1_load_and_audit()
    out = step2_core(df)
    print(out)


if __name__ == "__main__":
    main()
```

**像真系统的几个习惯**（面试官会注意到）：
- 常量放在顶部，不要在代码里散落魔法数字。
- 每个 step 是一个纯函数：输入 DataFrame，输出 DataFrame，方便单独测试。
- **数据质量计数器**：parsed / malformed / duplicate / out-of-order 每一项都 print 出来。
- **Invariant**：用 `assert` 检查不可能发生的状态，比如 book 不能 crossed、latency 不能为负、行数守恒。
- 注释只写 `# DECISION:`、`# TRADE-OFF:`、`# CHECK:` 三种。这正好对应你要讲的内容，写注释的同时也在练讲稿。
- 时间戳一律用 int64 纳秒做减法，最后再换算单位。

## 7. 卡住的时候怎么办

- **忘了语法**：说出你要做的语义，请面试官帮忙。
  > "I want to attach, for every order, the next inbound packet on the same session — that's an as-of join. I don't remember the exact `merge_asof` arguments; could you help me with the syntax?"
- **不确定方向**：给出两个选项，让面试官选。
  > "I see two ways forward: A is quicker but approximate, B is exact but needs a sort. Given the time, I'd do A first. Does that match what you're interested in?"
- **结果看起来不对**：直接说出来，并说你打算怎么检查。
  > "This p99 looks too high. Before trusting it, let me check for negative or duplicated timestamps."
- **时间快到了**：先停下来总结，再讲剩下的打算怎么做。

## 8. 这 5 题用到的 pandas / matplotlib 词汇（只记这些就够）

```python
# 读取与审计
pd.read_parquet(p)
pd.read_csv(p)
pd.read_json(p, lines=True)
re.compile(...).match(line)
df.info()
df.describe()
df.isna().sum()
df.duplicated(subset).sum()
(s.diff() < 0).sum()                      # 乱序计数

# 变形与配对
df.pivot_table(index="id", columns="event", values="ts", aggfunc="first")
a.merge(b, on="seq", how="outer", suffixes=("_A", "_B"))
pd.merge_asof(left, right, on="ts", by="session", direction="forward")

# 统计
df.groupby(keys)["lat"].quantile(PCTS).unstack()
df.groupby(keys).agg(n=("x", "size"), rate=("hit", "mean"))
pd.qcut(x, 10, labels=False)
np.searchsorted(sorted_ts, ts - window)   # 双指针：窗口内计数
np.average(x, weights=w)                  # 时间加权

# 画图
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
ax.plot(np.sort(x), np.linspace(0, 1, len(x)))   # ECDF
ax.set_xscale("log")
ax.step(t, bid, where="post")
ax.errorbar(x, y, yerr=...)
fig.savefig(path, dpi=110)
```

## 9. 练习计划（5 题 × 3 轮）

| 轮次 | 怎么练 | 目标 |
|---|---|---|
| Round 1（每题约 3 小时） | 跟着 `guide.md` 一步一步写，对照 `solution.py`，每个 `# DECISION` 都用英文说一遍 | 理解每一步为什么这么做 |
| Round 2（每题约 2.5 小时） | 只看 `problem.md` 和 step 函数名，自己写；卡住超过 10 分钟再看 guide | 不看答案也能写出来 |
| Round 3（每题 2 小时计时） | 空白文件、出声讲英文、录音或找人扮面试官，最后用 5 句话讲结论 | 面试状态 |

**建议日程**：
- 前 5 天：每天 1 题，做 Round 1。
- 接下来 3 天：做 Round 2，一天做 2 题。
- 最后 2–3 天：做 Round 3，挑 F1、F3、F2 优先。
- 面试前一天：只看每题 guide 的"收尾"和"可能的追问"，再过一遍第 5 节的 trade-off 表。

## 10. 通用追问速查
- **性能 6 问**（数据结构、内存与错误处理、测试、延迟指标、profiling、并发）：见 [F1 guide 的"性能追问"](F1_order_latency_from_logs/guide.md)。
- **网络基础**（TCP/UDP、multicast、Nagle、kernel bypass、PTP）：见 [extra_F5 guide 的网络部分](extra_F5_tcp_40ms/guide.md)。
- **做市与 markout**（stale quote、adverse selection、撤单 vs 改价、mass quote、MMP）：见 [F6 guide](F6_quote_pickoffs/guide.md)。
- **Order book 数据结构**（heap、sorted map、tick 数组）：见 [F2 guide](F2_orderbook_rebuild/guide.md)，也可以对照 `prep_code/01_order_book_matching.md`。
- **怎样正确比较两组**（配对、分层、标准化、Simpson's paradox）：见 [F3](F3_ab_feed_latency/guide.md) 和 [F4](F4_ioc_hit_rate/guide.md)。
