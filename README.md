# IMC QPE 冲刺 Study Guide

**岗位定位**（HR 原话）："quant research with a lens on **exchange infrastructure**"。
**面试建议**：用画图拆解数据，把方法和理由讲清楚；不考复杂算法。

**这套题怎么练**：
- 先在原始数据里找出问题，再识别混杂因素（confounder），最后得出可信的性能结论。这比把附加功能做得漂亮更有价值。
- 每道题的核心是 4 个 Step。2 小时内写出约 **100–120 行**清晰、能跑、验证过的核心代码，再花 2 分钟讲清楚**结论和 limitations**。
- F1 和 F2 另有一个加分的 **Step 5**（缓冲、优化、并发，约 35 行）：有时间就写，没时间就照 guide 口头讲。
- 每个 `solution.py` 最后都会打印 CONCLUSION 和 LIMITATIONS，照着练讲法。
- **答题模版和 5 张题卡**（澄清、拆 block、每块用什么方法和函数、trade-off、收尾话术）：[INTERVIEW_PLAYBOOK.md](INTERVIEW_PLAYBOOK.md)。
- 常见追问和 2–3 句的答法：[INTERVIEW_QA.md](INTERVIEW_QA.md)。

## 5 道题，按练习价值排序

排序标准：必须有真正的数据分析；不靠复杂算法、底层代码或复杂的统计方法；题目本身就要靠画图来发现问题、讲清结论。

| 优先级 | 题目 | 交易所基础设施的角度 | 关键分析点 | 图 |
|---|---|---|---|---|
| ⭐ 1 | [F3 A/B feed latency](F3_ab_feed_latency/problem.md) | multicast 的两条线路、交换机升级 | 两条线都丢的包；时钟漂移；**第二天流量更大是混杂因素**，所以要按包配对比较 | 延迟尾部、按负载分桶的配对差、延迟 vs 负载 |
| ⭐ 2 | [F1 Gateway log latency](F1_order_latency_from_logs/problem.md) | 下单路径：gateway 到交易所 | 解析日志、按订单配对事件；为什么均值不是答案；开盘时段要单独看；Step 5：流式解析、直方图合并、多进程 | 延迟尾部、gw-3 周期性卡顿、无响应时间线 |
| ⭐ 3 | [F4 IOC hit rate](F4_ioc_hit_rate/problem.md) | 在撮合引擎前的速度竞争 | 结构性断点不能混在一起算；**venue 构成是混杂因素**（Simpson's paradox） | 每日命中率、race curve、S1 vs S2 |
| 4 | [F2 Order book rebuild](F2_orderbook_rebuild/problem.md)，带 [mock 指南](F2_orderbook_rebuild/MOCK.md) | 交易所行情：seq、snapshot、L2 | 重复、乱序、缺号；价格转成整数 tick；缺号后标记 stale；和 truth 核对；Step 5：reorder buffer、吞吐、并发 | 数据健康度、BBO 加成交、spread 按事件 vs 按时间、imbalance 十分位 |
| 5 | [F5 TCP 40 ms](F5_tcp_40ms/problem.md) | 下单用的 TCP 连接 | 双峰分布；用对照组（s4）隔离出原因；用 hold 时间验证机制 | 按 session 的 ECDF、hold vs 下一个入站包 |

- **F3 排第一**：整道题几乎全靠画图和拆数据，核心思路是"直接对比会被流量误导，要配对比较"，最能体现方法论。
- **F1 第二**：最接近已知的 IMC 题型（"解析文本数据，算平均处理时间"）。
- **F4 第三**：纯 groupby 加置信区间，重点是把 Simpson's paradox 讲给 trader 听。
- **F2 降到第四**：它的核心是重建 book 的 replay 循环，算法和代码比重更大；但它是交易所基础设施的核心题，而且有现成的面试官剧本，所以留作 mock。
- **F5 第五**：分析简单、图少，结论要靠 TCP 知识。

没进前 5 的：F6（报价被 pick off）概念多、步骤长；P10 的异常检测要用 CUSUM 一类的方法；P06 是机器学习；P02、P07、P08 偏工程和算法，画图机会少。它们都在 [_archive/](_archive/README.md)。

画图基础在 [plotting/PLOT_GUIDE.md](plotting/PLOT_GUIDE.md)，配 D1–D4 四组 30 分钟的小练习。

## 冲刺顺序：写代码和画图穿插（约 6.5 小时，加 1 次 mock）

**Block 1：F3 A/B feed（约 2.25 h）**
1. PLOT_GUIDE §1–2（20 min）：讲图的 4 步，加选图速查表。
2. 画图练习 D1（30 min）：log histogram、ECDF、时间序列、hexbin。
3. F3 Step 1–2（40 min）：数据审计，按线路和日期算延迟，找出时钟漂移。
4. F3 Step 3–4（35 min）：直接对比为什么会误导 → 改成配对比较、按负载分桶 → 找出负载拐点。
5. 讲结论（10 min）：讲给 network team 听。

**Block 2：F1 gateway 日志（约 2 h）**
1. F1 Step 1–2（50 min）：解析日志、审计、按订单配对事件。
2. F1 Step 3–4（40 min）：分位数表（开盘时段单独看），诊断 gw-3 和 ARCX。
3. 画图练习 D3（30 min）：venue 对比、置信区间。
4. F1 Step 5（10 min）：只口头讲流式解析、直方图合并、多进程。

**Block 3：F4 IOC hit rate（约 1.75 h）**
1. F4 Step 1–2（40 min）：审计，每日命中率，找结构性断点。
2. 画图练习 D4（30 min）：上线前后对比、给结论。
3. F4 Step 3–4（35 min）：race curve；S1 vs S2 按 venue 拆开，发现 Simpson's paradox。

**Mock（约 1.5 h）**：先做画图练习 D2（30 min：step 图、离散柱状图、热力图），再请朋友照着 [F2 MOCK.md](F2_orderbook_rebuild/MOCK.md) 的 A 部分当面试官（60 min）。结束后，把最弱的那个 Block 用空白文件重做一遍。

**有余力再做**：F5（约 1 h，重点是两张图和机制解释）。

**面试前 30 min**：先过 [INTERVIEW_PLAYBOOK.md](INTERVIEW_PLAYBOOK.md) 第 1 部分的模版和各题卡的"报"，再看 PLOT_GUIDE §3–5（checklist、常见坑、英文句式），最后过一遍 [INTERVIEW_QA.md](INTERVIEW_QA.md)。

> **如果今天就要 mock**：先花 20 分钟读 MOCK.md 的 B 部分，再在 `F2_orderbook_rebuild/` 里运行一次 `python3 solution.py`，把 4 张图看熟。

每道题都有 `problem.md`（英文题面）、`guide.md`（中文分步指南）、`solution.py`（参考解答）和 `make_data.py`。

## 怎么跑

```bash
python3 generate_all.py                              # 生成全部数据（data/ 和 plotting/data/）
cd F3_ab_feed_latency && python3 solution.py         # 图在 ../plots/
```

旧的 10 道题、F6（报价被 pick off）和旧的 study guide 都移到了 [_archive/](_archive/README.md)，没有删除。
