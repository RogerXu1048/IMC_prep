# P06 参考答案 — Fill-Probability Model: Evaluation, Leakage, Shift

> 先自己做完再看。运行 `python3 solution.py` 可以看到全部输出，图在 `../plots/p06_*.png`。

## 1. 预设的 findings 和发现路径

| # | Planted finding | 怎么发现 | solution 里的数字 |
|---|---|---|---|
| 1 | **Leak：`resp_ns`**。交易所对"成交"要做 matching，响应更慢（~31 µs）；IOC 没成交直接 cancel，响应快（~21 µs）。这个值在结果之后才知道 | 按 outcome 看 `resp_us` 均值；单独一个 `resp_us` 的 AUC 就有 0.93 | 不含 resp：test AUC 0.68；含 resp：0.93（log loss 0.55 → 0.28） |
| 2 | `filled_qty`、`outcome` 本身就是 label | 定义 target 时就该排除 | — |
| 3 | **Random split 过于乐观** | 同一模型两种 split 对比 | random: AUC 0.735 / log loss 0.518；time split test: 0.682 / 0.549 |
| 4 | **Covariate shift（良性）**：day 11 S1 release，`log_reaction` 分布变了（PSI 0.69） | PSI/KS 表；adversarial validation days 1–10 vs 14–20 AUC 0.72，系数几乎全在 `log_reaction` 上 | 模型里有 latency 特征，所以它能外推，不一定是问题 |
| 5 | **假 shift（陷阱）**：如果用 UTC 算 time-of-day，11 月 1 日夏令时结束会让它"漂移"一小时（PSI 1.54） | 用 `America/New_York` 本地时间算 `tod_min` 后 PSI ≈ 0 | 正确特征 PSI 0.0004 |
| 6 | **Concept drift**：day 16 起 EX3 来了更快的竞争者，**输入分布没变，P(y\|x) 变了** | adversarial validation days 11–15 vs 16–20 AUC = 0.50（EX3 only 0.51）→ 输入没变；但按 day × venue 的 calibration gap：EX3 从 +0.06 跳到 **+0.48** | EX3 预测 0.83，实际 0.35 |
| 7 | **小的市场性变化**：days 11–15 所有 venue fade 率 +3pp（更波动的市场）→ 所有 venue calibration gap ≈ +0.03~0.07，day 16 后恢复 | calibration-gap 图 days 11–15 整体抬高 | 这是一种暂时性的 P(y\|x) 漂移（也可以看成跟 market regime 有关的 label shift），量级小 |
| 8 | **Per-venue base rate 比 global base rate 还差** | Part C 表格 | 因为 EX1 的 rate 在 release 后变了、EX3 在 day 16 后变了——不含 latency 的模型无法适应 covariate shift |

Remedy 对比（days 18–20 评估）：

| 方法 | overall log loss | EX3 log loss | EX3 mean pred vs actual |
|---|---|---|---|
| static (1–10) | 0.560 | 1.283 | 0.83 vs 0.35 |
| **static + per-venue Platt (16–17)** | **0.482** | **0.526** | **0.37 vs 0.35** |
| rolling retrain (13–17) | 0.504 | 0.726 | 0.62 vs 0.35 |
| recency-weighted (half-life 2d) | 0.500 | 0.689 | 0.60 vs 0.35 |

为什么 Platt 最好：这次的 drift 本质是 **EX3 的 intercept 下移**（竞争者变快 → 同样 latency 下赢的概率整体下降），per-venue Platt 只需估 2 个参数，2 天数据足够；rolling retrain 的窗口 13–17 里 3/5 的天还是旧 regime，所以 EX3 只"学了一半"。

## 2. Follow-up questions 参考答案

**Q1. Covariate shift vs label shift vs concept drift**
- **Covariate shift**：P(x) 变，P(y|x) 不变。例：release 后我们的 latency 整体变快（x 变了），但给定 latency 赢的概率没变。模型只要能外推（特征里有 latency），问题不大。
- **Label (prior) shift**：P(y) 变，P(x|y) 不变。例：市场很平静的一周，整体 fill rate 上升。可以用新的 base rate 调整 intercept。
- **Concept drift**：P(y|x) 变。例：EX3 来了新的快竞争者——同样的 latency、edge、venue，赢的概率掉了一半。只有看 label 才能发现；必须 recalibrate / retrain。

**Q2. AUC 不变但 calibration 坏了？**
AUC 只看 **排序**：任何单调变换（比如所有预测都加 0.3）AUC 不变。EX3 的情况：模型排序依然合理（快的、edge 小的更容易成交），但绝对概率整体高了 0.48。对 "send iff P·edge > cost" 这个 **阈值决策**，需要的是 **calibrated probability**：P 高估 → 我们会送出很多期望值为负的 IOC。所以这个场景 calibration（log loss、ECE、reliability）比 AUC 更重要。

**Q3. 为什么标准化统计量只能用训练集？**
用全数据的 mean/std 等于把测试期的信息（比如 release 后 latency 更快）泄漏进训练。实际上线时你也拿不到未来的统计量。原则：任何 "fit" 的东西（scaler、分箱边界、target encoding、填补值）都只在 train 上 fit，然后 apply 到 valid/test。solution 里 `FillModel.fit` 和 `binned_model` 的分箱边界都只用 train。

**Q4. 如果 fill rate 只有 2%？**
- Accuracy 毫无意义（全预测 0 就 98%）；用 log loss、PR-AUC、precision@k、calibration。
- 可以对负样本做 downsampling 加速训练，但 **必须把概率校正回来**（logit 加 log(采样率)），否则 calibration 错。
- 正样本少 → 每个 venue/bucket 的估计方差大，需要更强的正则或分层 pooling。

**Q5. 多久 retrain？窗口长短的 tradeoff？**
短窗口：适应快，但方差大（数据少）、容易被一次性噪声带偏；长窗口：稳定，但对 drift 反应慢。实践：长窗口训练主模型（稳定的结构，比如 latency 曲线形状），加一个 **短窗口的轻量校准层**（per-venue Platt/intercept），每天更新；再加 drift 报警触发人工检查。就是本题 Part E 的结论。

**Q6. Label 延迟或没有 label 怎么监控？**
- 没有 label 时：监控 **输入分布**（PSI/KS/adversarial）、**预测分布**（平均预测概率、送单率）、上下游的代理指标（比如交易所响应时间分布——它就是 leak 的那个变量，作为监控信号反而很好用！）。
- 有延迟 label 时：每天按 venue 做 calibration gap、log loss 的控制图。
- 本题的教训：**concept drift 在输入监控里完全看不见**（adversarial AUC = 0.50），只能靠 outcome 监控。

**Q7. `reaction_ns` 是合法特征吗？**
边界情况。它是订单离开 NIC 时才测到的，严格说在"决定是否发单"时还没有。但它由 **我们自己的系统** 决定、和结果无关（不是 leak），而且决策时可以用 **该 strategy / 代码路径的预期 latency**（比如过去 1 分钟的中位数）代替。上线版本应该用预期值；用实测值训练、用预期值预测会有轻微的 train/serve skew，要评估一下。对比：`resp_ns` 是 outcome 之后才有的，**绝对不能用**。

**Q8. 某个特征 PSI 很大要不要删？**
不一定。先问 **为什么** shift：
- 如果是已知、真实的变化（release 后 latency 变快）且 P(y|x) 不变 → 保留，模型正需要这个特征来适应。
- 如果是 pipeline bug / 定义变化（UTC vs 本地时间、单位从 µs 变 ns）→ 修 pipeline。
- 如果特征 shift 后模型在那个区域外推不可靠 → 考虑重训或加约束。
PSI 是报警信号，不是自动删特征的规则。

**Q9. 怎么测试 pipeline 本身？**
- Unit test：每个 metric 函数对已知小例子（AUC 和暴力两两比较的结果一致；完美预测 log loss≈0；ECE 对完美校准≈0）。
- Leakage test：打乱 label 后重新训练，test AUC 应 ≈ 0.5；时间 split 断言 `train.max_date < test.min_date`。
- 可复现：固定 seed、记录数据版本/hash 和代码版本；同一输入两次运行结果一致（regression test）。
- "Canary" 特征：加一个纯随机特征，系数应 ≈ 0。

## 3. 常见错误 / 扣分点
- 看到 AUC 0.93 就高兴——"too good to be true" 应该立刻触发 leak 检查。
- 用 random split 汇报结果；或者 time split 了但 scaler/分箱边界用全量数据 fit。
- 只报 AUC 不报 calibration；对一个阈值决策问题，这是核心错误。
- 用 UTC 计算 time-of-day，被夏令时坑（或者反过来，看到 PSI 很大就以为市场变了）。
- 看到 adversarial validation AUC = 0.72 就说"模型不能用了"——要看是 **哪个特征** 在 shift、以及模型能否处理它。
- 看到 adversarial validation AUC = 0.50 就说"一切正常"——concept drift 不改变输入分布。
- 只看整体 log loss 的日变化，不按 venue 拆开 → 定位不到 EX3。
- Remedy 评估时用了"还不可能拿到的 label"（比如用 day 18 的 label 校准再在 day 18 上评估）。

## 4. English speak-aloud scripts

**Leakage (Part A):**
> "Before modeling, I'll go column by column and ask: would I know this value at the moment I decide to send the order? The outcome and filled quantity are obviously the label. The exchange response time is subtler: it's measured after the exchange has processed our order, and a fill takes longer to process than a cancel, so it encodes the outcome. On its own it gives an AUC of 0.93, which is too good to be true. I'll exclude it — though it's a great monitoring signal."

**Splitting (Part B):**
> "I'd report the time-based split. In production we always predict tomorrow from yesterday, and this market is non-stationary — we had a release and the competitive landscape can change. A random split lets the model peek at the future regime, so its numbers are optimistic: here the AUC drops from 0.74 to 0.68 when I evaluate honestly."

**Concept drift (Part D):**
> "Adversarial validation between days 11–15 and 16–20 gives an AUC of 0.5, so the inputs look identical, yet on EX3 the model predicts 0.83 and we only fill 0.35. Same x, different y given x — that's concept drift, most likely a faster competitor on EX3. You can't detect that from input monitoring; you need outcome-based monitoring by venue. The cheapest fix is a per-venue recalibration on the latest labeled days."

## 5. 复杂度 / 实现要点
- IRLS logistic：每次迭代 O(N·p²) + O(p³)，p ≈ 15，~10 次迭代收敛 → O(N)。
- AUC：一次排序 O(N log N)（`rankdata`），不要用 O(N_pos·N_neg) 的两两比较。
- ECE / reliability：分箱 O(N)。
- PSI：用 reference 的分位数作为分箱边界，`searchsorted` O(N log B)。
- Adversarial validation：复用同一个 feature pipeline + logistic，O(N)。
- Platt scaling：每个 venue 一个 2 参数 logistic，O(N)。
