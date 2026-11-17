# IMC QPE 冲刺 Study Guide

**岗位定位**（HR 原话）："quant research with a lens on **exchange infrastructure**"。
**面试建议**：用画图拆解数据，并把方法和理由讲清楚；不考复杂算法；解释思路比提改进更重要。

所以每道题都按同一个结构练：先搞懂交易所基础设施的场景，再用 pandas 整理数据，配一个简单的算法点，用 matplotlib 画图，最后讲给交易员或工程师听。

## 保留的 5 道题

| 优先级 | 题目 | 交易所基础设施的角度 | pandas / 算法点 | 主要的图 |
|---|---|---|---|---|
| ⭐ 1 | [F2 Order book rebuild](F2_orderbook_rebuild/problem.md)，带 [mock 指南](F2_orderbook_rebuild/MOCK.md) | 交易所行情协议：seq、snapshot、L2 | 读 JSONL、数据审计；book 用 dict 加缓存的 best；sequencer 处理重复、乱序、缺号 | 数据健康度时间线、BBO step 图、spread 按事件 vs 按时间、imbalance 十分位 |
| ⭐ 2 | [F3 A/B feed latency](F3_ab_feed_latency/problem.md) | 交易所 multicast 的两条线路、交换机升级 | 按 seq 合并、算分位数、按负载分桶 | ECDF、时钟漂移、各线路抢先比例、延迟 vs 负载 |
| ⭐ 3 | [F1 Gateway log latency](F1_order_latency_from_logs/problem.md) | 下单路径：gateway 到交易所 | 用 regex 和 generator 解析日志、按订单配对事件 | log histogram、按 gateway 的 ECDF、延迟 vs 时间 |
| 4 | [F4 IOC hit rate](F4_ioc_hit_rate/problem.md) | 在交易所撮合引擎前的速度竞争 | 比例和置信区间、分层比较（Simpson's paradox） | 每日命中率、race curve |
| 5 | [F5 TCP 40 ms](F5_tcp_40ms/problem.md) | 下单用的 TCP 连接 | groupby 加配置表 join，定位根因 | 按 session 的 ECDF、hold 时间散点 |

⭐ 是冲刺必做的 3 道，4 和 5 有空再做。画图基础在 [plotting/PLOT_GUIDE.md](plotting/PLOT_GUIDE.md)，配 D1–D4 四组小练习。

## 冲刺顺序：写代码和画图穿插（约 7 小时，加 1 次 mock）

**Block 1：F2 order book（约 2.5 h）**
1. PLOT_GUIDE §1–2，20 min：讲图的 4 步，加选图速查表。
2. 读 MOCK.md 的 B.2–B.3，15 min：对着白板画流程图，出声讲 trade-off。
3. F2 Step 1–2，60 min：读数据、审计、写 `L2Book` 和 sequencer，和 truth 对比。
4. 画图练习 D2，30 min：step 图、离散柱状图、热力图。
5. F2 Step 3–4 加 `mock_plots.py`，30 min：4 张图，再用英文把收尾讲一遍。

**Block 2：F3 A/B feed（约 2 h）**
1. 画图练习 D1，30 min：log histogram、ECDF、时间序列、hexbin。
2. F3 Step 1–2，45 min：完整性检查、每条线路的延迟分位数和 ECDF、时钟漂移。
3. F3 Step 3–4，35 min：Step 3 只统计每个 seq 哪条线先到，跳过 live arbiter；Step 4 比较升级前后，分别做直接比较、同一个包配对比较、按负载分桶比较。
4. F3 Step 5，10 min：出声讲给 network team 听。

**Block 3：F1 gateway 日志（约 2 h）**
1. F1 Step 1–2，50 min：用 generator 加 regex 解析日志，按订单配对事件。
2. F1 Step 3–4，40 min：分位数表、gw-3 的周期性卡顿、ARCX 无响应、画图。
3. 画图练习 D3 或 D4，30 min：练沟通，比如置信区间、热力图、给结论。

**Mock，60 min**：朋友照着 [F2 MOCK.md](F2_orderbook_rebuild/MOCK.md) 的 A 部分当面试官。结束后，把最弱的那个 Block 用空白文件重做一遍。

**面试前 30 min**：PLOT_GUIDE §3–5（checklist、常见坑、英文句式），加上每道题 guide.md 里的"收尾"。

> **如果今天就要 mock**：先花 20 分钟读 MOCK.md 的 B 部分，再运行一次 `python3 solution.py && python3 mock_plots.py`，把 4 张图看熟。

## 每道题做哪些 / 跳过哪些

| 题 | 做 | 只口头讲或跳过 |
|---|---|---|
| F2 | Step 1–4 + `mock_plots.py` | Step 5：吞吐量和 5,000 个 symbol 的扩展 |
| F3 | Step 1、2、4、5；Step 3 只做谁先到 | live arbiter、bootstrap |
| F1 | Step 1–4 | Step 5：可合并的 histogram |
| F4 | Step 1–4 | Step 5："快 200ns 值多少钱"的插值估算 |
| F5 | Step 1–3、5 | Step 4：用 `merge_asof` 证明机制 |

每道题都有 `problem.md`（英文题面）、`guide.md`（中文分步指南）、`solution.py`（参考解答）和 `make_data.py`。

## 怎么跑

```bash
python3 generate_all.py               # 生成全部数据（data/ 和 plotting/data/）
cd F2_orderbook_rebuild && python3 solution.py && python3 mock_plots.py   # 图在 ../plots/
```

旧的 10 道题、F6（报价被 pick off）和旧的 study guide 都移到了 [_archive/](_archive/README.md)，没有删除。
