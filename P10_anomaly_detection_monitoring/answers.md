# P10 答案与讲解：监控指标的 Anomaly Detection

> 先自己做一遍，再看这里。参考代码在 `solution.py`，运行 `python3 solution.py` 会打印下面提到的所有数字，并在 `plots/p10_*.png` 生成图。

## 1. 预设的 findings 和发现路径

| # | 预设的现象 | 怎么发现 |
|---|---|---|
| 1 | **数据质量**：srv-6 在 11-03 有 30 行重复（agent 重启后重发，cpu 值略有不同）；srv-5 在 10-28 缺了 20 分钟（11:30–11:50）；另有 8 个 p99 是 NaN | 统计 `df.duplicated(["server","ts"])`，发现全列去重抓不到这 30 行；按 server-day 计数，发现少于 390 行的天 |
| 2 | **Intraday U 型季节性**：开盘前 15 分钟 p99 的中位数约 41µs，午间约 18µs，原因是 msg_rate 高 | 按 minute 做 groupby 画 profile；Spearman(rate, p99) 约 0.83 |
| 3 | **全市场 burst**：11-02 10:00–10:07（经济数据发布），6 台 server 同时 rate ×3，p99 跟着升高。这**不是** incident | naive detector 在同一分钟对 6/6 台 server 报警；load-adjusted 之后的 residual 是平的 |
| 4 | **14 个 latency spike + 5 个 packet-drop burst**（有 label 的 acute incident） | load-adjusted robust z 加 persistence，drops 用 Poisson tail |
| 5 | **Level shift**：srv-4 从 **11-05** 起 p99 ×1.25、p50 ×1.15，一直不恢复（像一次 deploy regression） | 按天算 residual median 画 heatmap，或用 CUSUM；第 9 天开始一整行变红 |
| 6 | **Gradual drift**：srv-2 在 **11-10、11-11** 两天 p99 随时间线性上升，收盘时约 ×2.5，第二天开盘恢复（像 memory leak，隔夜重启后恢复） | holdout 上的 "false positives" 全部集中在 srv-2 这两天下午；看日内斜率，open→close 的 residual 涨了 +0.9 log |

### 分析路径（面试时的推荐顺序）
1. **先看数据质量**，再看分布：p99 偏度 4.7，取 log 之后降到 1.4，所以在 log space 建模。
2. **画 intraday profile**，发现 U 型；再画 p99 对 msg_rate 的 log-log 散点图，会看到一条单调上凸的曲线，即 queueing。所以"正常 latency"取决于负载。
3. **复现 baseline**（Part B）：train 上 114 个 event，105 个是 FP，precision 0.08；68% 的 FP 集中在开盘 20 分钟。这一步是用数字说明现有方案为什么不行。
4. **改进**：对每台 server 拟合 `log p99 ~ poly2(log rate)`（只用 train 中没有 incident 的分钟），residual 用 median/MAD 标准化成 robust z，再加 persistence（连续 2 分钟 > k，或者单分钟 > 2k）。drops 是 count data，按负载分位数估计 Poisson λ，超过 99.999% 分位数就报警。
5. **调参与评估**：k 在 train 上扫一遍，k=8 时 F1=0.92；在 holdout 上 recall 7/7，precision 0.44。
6. **复核 holdout 上的 FP**：9 个 FP **全部**是 srv-2 在 11-10、11-11 的下午。它们其实是真问题，只是 on-call 没有标。这里就自然引出 Part E。
7. **Chronic**：画 daily median residual 的 heatmap，srv-4 从 11-05 起整行变红（step），srv-2 在 11-10、11-11 两格变红（drift）。CUSUM 分别在 11-05 和 11-10 报警。日内斜率能区分 step 和 trend。
8. **Tail fitting**：residual z 的分布用 Student-t（df≈3.9）拟合，AIC 最好。正态分布对 P(z>4) 的估计低了约 8 倍。"3σ 就够罕见了"的直觉在 heavy tail 下不成立。

## 2. Follow-up questions 参考答案

**Q1. 为什么 raw p99 上的全局 z-score 不好？**
- 没考虑**季节性和负载**：开盘、收盘、新闻 burst 时 latency 高是正常的。
- mean/std 会被 outlier 拉偏，heavy tail 下 "3σ" 的误报率远高于 0.27%。
- 单分钟 noise 会触发报警，没有 persistence。
- 全市场事件会让所有 server 一起报警，这不是 server 本身的问题。

**Q2. 为什么用 median/MAD？MAD 什么时候会失效？**
- breakdown point 是 50%：最多一半数据被污染，估计也不会被带偏；mean/std 的 breakdown point 是 0。
- MAD 失效的情况：(a) 数据离散、大量相同值，MAD=0 时 z 会变成无穷大，要设下限；(b) 分布严重偏斜，对称的 MAD 会低估一侧的尾部，可以先做 log 变换或者分别算上下 MAD；(c) 异常比例超过 50%，比如大部分时间都在 incident 状态。

**Q3. Static 还是 rolling baseline？**
- static baseline 能检测到 level shift（srv-4 会持续偏高），但"新常态"会被一直当成异常，造成告警疲劳。
- rolling baseline（比如前 5 天的 median）会适应 shift。shift 只会在头几天被报出来，之后被吸收；缺点是 slow drift 会被"学进去"，永远检测不到，而且污染的数据会进入 baseline。
- 实践中两层一起用：短期 detector 用 rolling baseline，管 acute incident；长期 detector（CUSUM 或日对日的比较）管 chronic 变化，结果发到 dashboard 或 ticket，而不是 page。另外，被判为 incident 的分钟不要进入 baseline。

**Q4. Point-wise 还是 event-wise 评估？**
On-call 关心的是 **page 的数量**（每个 event 一次 page）和 **多快发现**（detection delay）。一个 10 分钟的 incident 检测出 9 分钟还是 10 分钟，对他们来说没区别。point-wise 指标会偏向长 incident，也会把一个 30 分钟的 FP 算成 30 个错误。所以用 event-level precision/recall + delay；如果要报 point-wise，作为补充即可。

**Q5. 完全没有 label 怎么选阈值？**
- 定一个**告警预算**，比如每天最多 1 个误报 page；然后用 clean 数据上 score 的经验分布或拟合的 tail 分布（Part F）反推 k。
- 注入合成异常（synthetic injection）来估计 recall：在历史数据上人为放大 p99 ×2、×3，看能不能抓到。
- 让 on-call 回头标注一小批 alert（active learning）。

**Q6. FP 和 FN 的成本不对称时怎么办？**
把目标从 F1 换成 expected cost：`cost = n_FP × 10min × engineer_rate + n_FN × $X`，在 train 上扫 k，取 cost 最小的点。FN 越贵，k 越小。也可以分两档：k 高的进 page，k 低的只发 Slack 或 ticket。

**Q7. 60 个 per-second p99 的平均值是不是分钟的 p99？**
**不是。** 分位数不能平均，不可加。比如 59 秒都很快、1 秒极慢，真实的分钟 p99 由那 1 秒决定，平均值会把它稀释掉。正确的做法是让 agent 上报**可合并的 sketch**：固定 bucket 的 histogram（HDRHistogram）或 t-digest。分钟级、天级、跨 server 的分位数都由 merge 后的 histogram 计算。

**Q8. 2000 台 server 实时怎么跑？**
每台 server 保存的状态：模型系数（几个 float）、baseline median/MAD、上一分钟的 z（给 persistence 用）、CUSUM 累计值。每条更新 O(1)：查表算 expected、算 z、更新 CUSUM。用 server 当 key 做 partition（Kafka partition 或者 Flink keyBy），天然可以水平扩展；模型参数每晚离线重算一次。

**Q9. 多台 server 同时报警？**
有两种可能：一是共同原因（市场事件、上游 feed、交换机或机架故障），二是 detector 没有正确扣除 market-wide 的负载。处理方法：(a) 加一个 "market-wide" 特征，比如所有 server residual 的中位数，先扣掉；(b) 做告警聚合，把 N 台 server 同时报警合并成一个 "cluster event"，发给 network 或 market-data 团队，而不是给每台 server 的 owner 发 N 个 page。

**Q10. 测试怎么做？**
- Unit test：`flags_to_events` 遇到 gap=2 或 3 分钟时的分段边界、跨 server 的边界；`evaluate` 的匹配规则；MAD=0 时的保护。
- Synthetic injection test：在干净数据上注入已知 spike，必须能被检测到，同时干净数据上的报警数要在预算内。
- Regression test：固定 seed 的数据跑完，event 表的 hash 和 golden file 一致。改模型的时候，diff 要能解释清楚。

## 3. 常见错误 / 扣分点
- 用 `drop_duplicates()` 全列去重，漏掉 30 行重发数据；或者把缺失的分钟填成 0，造成假的 "latency 骤降"。
- 在 holdout 上调阈值（data leakage），或者先看了 holdout label 再"改进"模型。
- 只报 point-wise accuracy。这里 99% 以上的分钟都是正常的，全猜"正常"也有 99% accuracy，指标没有意义。
- 把 market-wide 的 10:00 burst 当成 incident 去 page。
- 看到 holdout 上 precision=0.44 就说"模型不好"，没有去看 FP 是什么。实际上 FP 全是真问题（srv-2 的 leak）。**一定要看具体例子**。
- 用 rolling baseline 却不排除 incident 分钟，结果 baseline 被污染。
- Tail 用正态假设算误报率，低估约一个数量级。

## 4. English speak-aloud scripts

**Framing (start of interview):**
> "Before building anything, I want to know what normal looks like. Latency here is driven by load, so a fixed threshold will fire every morning at the open. My plan is to model expected latency as a function of message rate, look at the residual on a log scale with robust statistics, and evaluate on alert events rather than minutes, because that's what the on-call engineer experiences."

**Reporting the evaluation:**
> "On the holdout days the new detector caught all seven labelled incidents with zero delay. The baseline produced 68 pages and the new one 16. All nine of the remaining 'false positives' are srv-2 on two afternoons, and when I look at them, p99 climbs steadily through the day and resets the next morning. That looks like a memory leak nobody labelled, so I'd count them as true findings and flag them to the infra team."

**Communicating to infra/trading:**
> "Two chronic issues: srv-4 has been about 25% slower at p99 since November 5th, which is consistent with a deploy that day, and srv-2 degraded gradually on Nov 10–11 and recovered overnight both times. I'd suggest checking the srv-4 release diff and looking at srv-2's memory profile. Neither shows up in the per-minute pages, so I'd add a daily CUSUM report as well."

## 5. 复杂度
- 拟合：每台 server 做一次 polyfit，O(n)；robust z 是 O(n)；`flags_to_events` 因为排序是 O(n log n)。
- `evaluate`：O(E·I)，E 和 I 都很小；规模大时可以按 server 分组后用 interval sweep，降到 O((E+I) log(E+I))。
- 在线版本：每条更新 O(1) 时间，每台 server O(1) 内存。
