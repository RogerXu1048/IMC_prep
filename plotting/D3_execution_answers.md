# D3 答案：各 venue 的执行质量

参考代码：`D3_execution_solution.py`。图在 `plots/d3_q*.png`。

## Q1 哪个 venue 成交率最好 → 排好序的点图，带 95% CI，标出 n（`d3_q1_fill_rate.png`）
- **为什么用它**：比较几个类别的同一个比例。柱状图的 y 轴必须从 0 开始，但这里的差别都在 80–92% 之间，从 0 开始就看不出差别；把轴截断又会夸大差距。所以改用**点图**，可以放大坐标轴。按成交率排序，方便读名次。**一定要画置信区间并标出 n**。
- **讲法**："A fills best at about 92%. E looks worst, but it only has 169 orders, so its confidence interval is about ±6 points and overlaps D. I wouldn't rank E yet. D is reliably worse than A, B and C."
- **坑**：不画 CI 就会得出"E 最差，减少路由"这种被小样本误导的结论。颜色在这里不承载信息，用一种颜色就够了。

## Q2 订单大小影响滑点吗 → 散点图（抽样、半透明，x 轴 log），叠加分桶的 median 和 p90（`d3_q2_slippage_vs_size.png`）
- **为什么用它**：两个连续变量的关系，用 scatter。数据量大，所以抽样 5k 个点、调低透明度，x 轴用 log（订单大小跨了两个数量级）。单看散点很难判断趋势，所以按大小分桶，叠加 median 和 p90 两条线，把"典型情况"和"坏情况"都画出来。
- **讲法**："Slippage rises with size, roughly with the square root. The median goes from about 1 to 2 bps, and the p90 rises too, so large orders are both more expensive and less predictable."
- **坑**：极端值会把 y 轴拉得很大，所以截了 y 轴范围，要说明截了。分桶用等频（`qcut`），保证每个桶的样本量够。

## Q3 某个时段某个 venue 表现差吗 → 热力图：venue × 30 分钟时段，画**未成交率**（`d3_q3_heatmap.png`）
- **为什么用它**：两个类别维度对应一个数值，用热力图。指标选"未成交率"而不是成交率，这样颜色越深就越糟，一眼能看到问题。E 的样本太少，去掉并说明原因。
- **讲法**："D misses about 38% of its orders in the first 30 minutes and is roughly normal after that. That accounts for most of D's lower overall fill rate in Q1, so it's an opening-auction or open-liquidity issue at D, not a general one."
- **坑**：如果换成 venue × 时段的分组柱状图，13 × 4 = 52 根柱子，没法读。

## Q4 路由组合怎么变 → 100% 堆叠面积图，每天各 venue 的份额（`d3_q4_mix.png`）
- **为什么用它**：组成部分占整体的比例随时间变化，用 100% stacked area（或者 stacked bar）。颜色固定对应 venue，和其他图一致。
- **不用什么**：不要用 20 个饼图，也不要用 5 条线画份额（看不出加起来是 100%）。如果要精确比较某一个 venue，可以单独画它的份额折线。
- **讲法**："C's share roughly doubled, from about 15% to about 35% of routed shares, and almost all of it came from A. B and D are flat."
- **坑**：只有贴着 x 轴的最底层能精确读数，中间层的变化不好读。关键的那个 venue，最好放在底层或者单独画。

## Present（先带哪个结论）
先讲 **Q3 + Q1**："D's fill problem is concentrated in the first 30 minutes, so we should route around D at the open." 这个结论能直接采取行动，证据也清楚。第二点再提 Q4：份额正在转向 C，要确认这是不是有意为之。
