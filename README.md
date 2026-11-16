# IMC QPE Technical Interview Prep：Final 5

这是 IMC Trading Quant Performance Engineer 面试 technical 轮（"data analysis / algorithm design task in Python"）的 5 道模拟题。每道题按 2 小时设计，同时考数据分析和一个算法点，用 pandas + matplotlib 完成，场景对应 exchange technology、latency research 和 market microstructure。

| # | 题目 | 核心 |
|---|---|---|
| F1 | [Order latency from gateway logs](F1_order_latency_from_logs/problem.md) | 流式解析日志、事件配对、延迟分位数、找周期性卡顿 |
| F2 | [Order book rebuild from a JSON feed](F2_orderbook_rebuild/problem.md) | L2 book 加 sequencer（去重、乱序、缺号）、spread、imbalance |
| F3 | [A/B feed latency & a network change](F3_ab_feed_latency/problem.md) | A/B arbitration、时钟漂移、配对比较、延迟 vs 负载 |
| F4 | [IOC hit rate vs latency](F4_ioc_hit_rate/problem.md) | 命中率与置信区间、Simpson's paradox、200ns 值多少钱 |
| F6 | [Quote pick-offs & markouts](F6_quote_pickoffs/problem.md) | 做市 markout、stale fill、requote 排队、race curve |
| extra | [TCP 40 ms mystery](extra_F5_tcp_40ms/problem.md) | Nagle + delayed ACK，以及网络基础问答 |

## 快速开始

需要 Python 3.10+，以及 numpy、pandas、matplotlib、scipy、pyarrow。在 Python 3.12 + pandas 3.0 上测试过。

```bash
python3 generate_all.py            # 生成 F1–F6 的数据到 data/（固定 seed，不进 git）
cd F1_order_latency_from_logs
python3 solution.py                # 参考解答，图存到 plots/
```

`extra_F5_tcp_40ms` 的数据需要在它自己的目录里运行 `python3 make_data.py`。

## 每个题目目录里有什么

- `problem.md`：英文题面，面试时只看这个。
- `make_data.py`：生成带"脏数据"的模拟数据集。
- `solution.py`：精简的参考解答（约 170–220 行），注释只用 `# DECISION` / `# TRADE-OFF` / `# CHECK` 三种标签。
- `guide.md`：中文分步指南，包括时间分配、澄清问题、每一步的决策与 trade-off、代码骨架、自检数字、追问和英文讲稿。

**从 [STUDY_GUIDE.md](STUDY_GUIDE.md) 开始。** 建议顺序是 F1 → F3 → F2 → F4 → F6。

> 所有数据都是合成的，题目是模拟练习，不是 IMC 的真题。
