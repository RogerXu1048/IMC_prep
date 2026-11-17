# P05 参考答案 — IOC Hit Rate vs Latency

> 先自己做完再看。运行 `python3 solution.py` 可以看到全部输出，图在 `../plots/p05_*.png`。

## 1. 预设的 findings 和发现路径

| # | Planted finding | 怎么发现 |
|---|---|---|
| 1 | **脏数据**：0.1% 完全重复的记录；~0.1% `reaction_ns` 缺失；~0.04% `reaction_ns` 为负（NIC clock step） | `drop_duplicates()`、`isna()`、`<= 0`。注意：这些行的 **outcome 仍然有效**，算 hit rate 可以保留，只是做 latency 分析时剔除 |
| 2 | **Day 16（11-09）EX3 hit rate 从 ~77% 掉到 ~35%**，两个 strategy 都掉，其他 venue 不变，我方 latency 也没变 → 外部原因：EX3 上来了一个更快的新竞争者（中位数 ~1.05 µs） | Part A 的 daily × venue 图；用 day-over-day 的 z-score 自动标出（z ≈ −19） |
| 3 | **Day 11（11-02）S1 release**：S1 median reaction 1.40 → 1.10 µs（−300 ns） | daily 图里 EX1/EX2 跳升；Part E 里按 period 看 S1 latency 中位数 |
| 4 | **Hit rate 随 reaction time 单调下降**，曲线形状 = (1−fade)·P(竞争者比我们慢)；不同 venue 竞争者速度不同：中位数约 1.6 / 2.2 / 2.8 / 3.5 / 5.0 µs | Part B 分箱图；Part D 结构模型拟合出 1.54 / 2.15 / 2.79 / 3.59 / 5.08 µs |
| 5 | **Simpson's paradox**：S2（慢，median 3.2 µs）整体 hit rate 74.8% > S1（快，1.4 µs）67.9%，但 **每个 venue 里 S1 都远高于 S2**。原因：S2 85% 的单在 EX5（竞争最慢），S1 41% 在 EX1（竞争最快） | venue × strategy 交叉表 + venue mix 表；直接标准化：S2 用 S1 的 venue mix 只有 21.8% |
| 6 | **Release 效果被混杂**：(a) release 之后 S1 把更多单子放到 EX1（更难的 venue），(b) days 11–15 市场更波动，所有 venue fade 率 +3pp。naive before/after = +7.4pp，按 venue 分层 = +8.8pp，再用 S2 做对照的 DiD = **+11.1pp**（真实效果约 +11pp） | 分层 + diff-in-diff，窗口只用 days 1–15 |
| 7 | **edge 越大竞争越激烈**：logistic 里 `edge_ticks` 系数为负（−0.21） | logistic regression 系数 |

关键分析路径（面试里要说出来的顺序）：
1. **先清洗、先画按天按 venue 的图**——这一步就能看到 day 16 EX3 的断崖。好的 candidate 会立刻说："这是一个 regime change，后面的分析要么剔除它，要么单独建模。"
2. 再做 latency 曲线、Simpson、模型、因果。每一步都要说明用了哪个时间窗口、为什么。

## 2. Follow-up questions 参考答案

**Q1. 为什么用 Wilson interval？**
Normal/Wald 区间 `p ± 1.96·sqrt(p(1−p)/n)` 在 p 接近 0 或 1、或 n 小时很差：覆盖率低于 95%，还可能超出 [0,1]（比如 p=0 时宽度为 0）。Wilson 是对 score test 反解出来的，n 小、p 极端时表现好。n 很大、p 在中间时两者几乎一样。

**Q2. Attempts 独立吗？**
不独立。同一时刻、同一 underlying 的机会相互相关（市场波动时所有 attempt 一起变难）；同一天内也有共同冲击（比如 days 11–15 的 fade 上升）。后果：binomial CI **过窄**。修正：按天（或按小时）做 **block / cluster bootstrap**，或用 cluster-robust standard errors。Solution 里 DiD 的 CI 就是按天 block bootstrap。

**Q3. logit 对 reaction time 线性对吗？**
不一定。机制模型说 P(hit) = (1−fade)·S_C(t)，有一个 (1−fade) 的上限 plateau，而 logistic 会趋向 1。检验方法：画分箱的经验 hit rate vs 模型预测（calibration by bin）；加 `log(t)`、样条、或 venue × latency 交互项；比较 log-likelihood/AIC。我们的数据里 EX5 曲线的 plateau 在 ~0.93，就是 fade 的上限。

**Q4. edge_ticks 怎么混杂 latency 效果？**
大 edge 的机会吸引更多、更快的竞争者 → hit rate 低。如果我们在大 edge 机会上反应也更慢（比如更复杂的定价路径），那么不控制 edge 会 **高估** latency 的负面影响；反过来如果大 edge 走更快的路径，会低估。所以回归里要放 edge，或者按 edge 分层。

**Q5. 快 200 ns 的收益会持续吗？**
不会永久。这是一场 arms race：竞争者也在优化，今天的优势会被侵蚀（就像 day 16 EX3 上出现了新竞争者）。另外获得的额外 fills 可能是 **edge 更小或 adverse selection 更高** 的那部分。所以估算值是"静态、竞争者不反应"假设下的上限，应该标注 half-life。

**Q6. 能从我们自己的数据推断竞争者 latency 分布吗？**
可以，这是本题最"QPE"的地方：如果我们赢 ⟺ 我们的到达时间 < 最快竞争者到达时间（且没 fade），那么 hit rate 曲线 h(t) = (1−f)·P(C > t)，**h(t)/(1−f) 就是竞争者 latency 的 survival function**。我们自己的 latency 有自然变化（jitter、不同代码路径），相当于在不同 t 上"采样"了这条曲线。假设：(1) 我们的 latency 与竞争者 latency 独立；(2) fade 与 latency 无关；(3) 不同 t 的机会在其他方面可比（edge mix 等）——不然要按 edge 分层；(4) 拟合出来的 C 其实包含了双方的 exchange-side jitter 和 edge mix，所以 sigma 偏大。

**Q7. 怎么设计实验？**
随机化：对一部分 attempt **人为加 delay**（比如随机加 0/100/200/300 ns），这是一个干净的 A/B test，直接得到 dP(hit)/dt，没有混杂。代价是故意损失一些 fills，所以只在小比例、低 edge 的机会上做。也可以做 switchback（按时间段轮换配置）。

**Q8. hit rate 上升但 P&L 下降？**
Winner's curse / adverse selection：我们赢到的可能恰好是"别人不想要的"单——例如对手知道价格要变，主动不去抢；或者 quote 没有 fade 是因为 market maker 认为它不再 stale。还有：多出来的 fills 都是小 edge 的；手续费/rebate 结构变化；fill 之后对冲成本上升。所以要看 **markout P&L**（成交后 1s/10s/60s 的 mid 变化），而不仅是 hit rate。

**Q9. 怎么监控，当天就发现某 venue 的突变？**
每个 venue × strategy 维护实时 hit rate，用 **控制图 / CUSUM / binomial z-test** 跟过去 N 天同时段的基线比较（注意日内季节性）；同时监控我方 latency 分布，**如果 hit rate 掉了而我方 latency 没变 → 外部原因（竞争者/交易所变化）**；如果我方 latency 变了 → 内部原因。报警附带 drill-down（venue、underlying、edge bucket）。

## 3. 常见错误 / 扣分点
- 不清洗就算：重复记录会让 CI 略窄；负 latency 进入 qcut 或 log 会直接报错或产生 NaN。
- 把缺失 latency 的行整行删掉——它们的 outcome 是有效的，删掉会对 hit rate 引入偏差（如果缺失和结果相关）。
- 没画按天的图，导致 day 16 之后的 EX3 regime change 混进所有结论（比如用 days 1–20 做 release 的 before/after 得到 +6.0pp，严重低估）。
- 用等宽分箱且不看样本量，尾部箱子只有几十个样本却画得很"确定"。
- 看到 S2 整体更高就下结论；或者知道 Simpson's paradox 但说不出 **为什么**（venue mix + venue 难度差异）。
- 把 logistic 系数直接当概率变化（要转换成 marginal effect，或者说 odds ratio）。
- 估算 $ 价值时不说假设（edge 全部兑现、无 adverse selection、竞争者不反应、填满数量）。
- 因果问题上只做 naive before/after，不考虑同期的市场变化（需要 control group）和 mix shift（需要分层）。

## 4. English speak-aloud scripts

**Opening (Part A):**
> "Before computing anything, I want to pin down the metric. I'll use a count-based hit rate — an attempt counts as a hit if it got any fill — and I'll also report the quantity-based fill rate, since partial fills exist. Then I'll check data quality: duplicates, missing and negative latencies. Rows with bad latency still have valid outcomes, so I'll keep them for the hit-rate tables but exclude them from latency analysis. And the first plot I want is the daily hit rate per venue, because any regime change will contaminate everything downstream."

**Simpson's paradox (Part C):**
> "S2's overall hit rate is higher, but within every single venue S1 wins more often. The reason is venue mix: 85% of S2's attempts are on EX5, where the competition is slow, while S1 spends 40% of its attempts on EX1, the most contested venue. If I re-weight S2 to S1's venue mix, its hit rate drops to about 22%. So the aggregate comparison is confounded by venue, and the fair comparison is within venue or standardized."

**Value of latency (Part D):**
> "Mechanically, we win when we arrive before the fastest competitor and the quote hasn't faded. So the hit-rate curve is one minus the fade probability, times the survival function of the competitor's latency. Our own latency varies naturally, which lets us trace out that curve and fit a lognormal per venue. Then the value of 200 nanoseconds is just the change in predicted hit probability for each attempt, summed and multiplied by size and edge — under the assumptions that we capture the full edge and competitors don't react."

## 5. 复杂度 / 实现要点
- 所有 groupby 聚合：O(N)（hash groupby）；qcut 分箱：每个 venue 一次排序 O(N log N)。
- IRLS logistic：每次迭代 O(N·p²) 构建 Hessian + O(p³) 求解，p=7，几次迭代收敛 → 实际 O(N)。
- 结构模型 MLE：每次 likelihood 评估 O(N_v)，Nelder–Mead 几百次评估，5 个 venue，总共几秒。
- Day-block bootstrap：先把数据聚合成 day × strategy × venue 的计数表（20×2×5=200 个 cell），再重采样天 → 每次 bootstrap 只处理 200 行而不是 17 万行，这是一个值得在面试里说的 **performance trick**（先 aggregate 再 resample）。
