# D4 答案：策略 PnL 复盘

参考代码：`D4_pnl_solution.py`。图在 `plots/d4_q*.png`。

## Q1 各策略表现 → 累计 PnL 折线，直接在线尾标注名字，标出 deploy 日（`d4_q1_cumulative.png`）
- **为什么用它**：表现随时间变化，用**累计** PnL，因为每日 PnL 噪声太大，4 组每日柱子叠在一起没法读。4 条线以内，名字直接写在线尾，不用看图例。deploy 日画一条竖线。
- **讲法**："S1 and S2 grind up steadily, to $2.5M and $2.1M. S3 swings wildly and ends at minus $0.4M. S4 made $0.9M but went flat right after its deploy."
- **坑**：累计曲线会让后期的变化看起来"更大"；要比较前后两段，看斜率，不要看高度。

## Q2 S3 风险多大 → 每日 PnL 的 histogram，加回撤（underwater）图（`d4_q2_s3_risk.png`）
- **为什么用它**：风险有两面。一是单日的分布：histogram 能看到肥尾，最差 −$420k、最好 +$471k，而通常每天的波动只有约 ±$85k。二是连续亏损：回撤图能看到最深跌到 −$1.47M，而且一直没回来。
- **讲法**："S3's daily PnL has fat tails: there are days five times the typical move in both directions. Its drawdown reached $1.47M and hasn't recovered. A volatility number alone would understate this risk."
- **坑**：只报 Sharpe 或标准差会掩盖肥尾。回撤图用面积填充，从 0 往下画。

## Q3 S1 的 PnL 从哪来 → 横向柱状图，按绝对值排序，取前 12 个加上 "Other"，亏损的用另一种颜色（`d4_q3_attribution.png`）
- **为什么用它**：比较 40 个类别的大小，用排好序的横向柱状图，名字放在左边好读。把长尾合并成 "Other"。正负用两种颜色，0 轴一眼可见。
- **不用什么**：不要用饼图：40 块根本没法读，而且饼图画不了负数。
- **讲法**："S1 is concentrated: the top 5 symbols make 79% of its PnL. Two symbols, SYM12 and SYM06, lose a meaningful amount, about $210k and $120k. I'd check whether those are a model issue."
- **坑**：按绝对值排序，亏损的大户才会出现在前面。如果按数值排序，亏损的会被排到最后，或者被并进 Other。

## Q4 策略之间能分散风险吗 → 相关系数热力图，用发散配色、以 0 为中心，格子里写数值（`d4_q4_correlation.png`）
- **为什么用它**：相关矩阵本身就是一个二维表，热力图是标准画法。相关系数有正有负，所以用**发散**配色：0 是白色，红蓝两极。4×4 的格子很少，可以直接写数值。
- **讲法**："S1 and S2 have a correlation of about 0.7, so together they're less diversified than four strategies suggest. S3 and S4 are roughly independent of everything."
- **坑**：用单色系画相关系数（负相关就看不出来了）；在 30×30 的大矩阵里每格都写数字。

## Q5 S4 deploy 有没有伤害 → 左边：每日柱子加 10 日滚动均值，加 deploy 竖线；右边：前后两段的 box plot，标出 n（`d4_q5_before_after.png`）
- **为什么用它**：before/after 问题要同时看"随时间怎么变"（左）和"两段的分布对比"（右）。滚动均值把噪声平滑掉。box plot 标出 n，提醒样本量不对等（80 天 vs 40 天）。
- **讲法**："Average daily PnL fell from about +$10k to +$3k after the deploy. But daily noise is about $25k, so with 40 days the difference is only around 1.5 standard errors. It's a warning sign, not proof. I'd keep monitoring, or A/B test the old and new versions on a subset of symbols."
- **坑**：只看左图会被眼睛骗，右图的箱子是重叠的。**说清楚自己有多大把握**，正是这道题考的地方。

## Present（开场四句话）
> "S1 and S2 delivered most of the period's PnL, $4.6M together, but they're about 70% correlated, so they're really one bet. S1's PnL is concentrated, with the top 5 symbols making 79% of it, and two symbols losing consistently. S3 ended negative after fat-tailed losses and a $1.5M drawdown, so its risk limits need review. S4 has gone flat since its new version, which isn't conclusive yet but is worth an A/B test."
