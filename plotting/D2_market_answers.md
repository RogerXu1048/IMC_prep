# D2 答案：一天的行情

参考代码：`D2_market_solution.py`。图在 `plots/d2_q*.png`。

## Q1 价格怎么走 → 1 分钟 mid 的折线，标出事件（`d2_q1_price.png`）
- **为什么用它**：价格随时间变化，用 line plot。整天有 23,400 个 1 秒数据点，太密，所以先 resample 到 1 分钟。用 mid，不用 bid 或 ask 单边，因为 mid 才代表"价格"。10:00 的事件用一条竖线加文字标注出来。
- **讲法**："Price gapped down about 1.5% at 10:00 and then won back roughly half of it by the close."
- **坑**：x 轴只显示 HH:MM，同一天不需要日期。y 轴不需要从 0 开始：折线看的是变化，柱状图才必须从 0 开始。

## Q2 放大 10:00 → bid/ask 用 step 图，成交量用 bar 放在下面的 panel，共用 x 轴（`d2_q2_zoom.png`）
- **为什么用它**：报价在下一次更新之前一直保持不变（piecewise constant），所以用 `step(where="post")`。普通折线会在两次更新之间画出斜线，好像中间有过那些价格，这是错的。成交量和价格单位不同，所以放在**另一个 panel**，共用时间轴，而不是用双 y 轴。
- **讲法**："At 10:00:00 the book moved down about $1.50 in 20 seconds. The spread widened from 1 tick to 5–10 ticks for about 40 seconds, and volume jumped roughly 15 times. That looks like a news-driven repricing, with liquidity pulled while it happened."
- **坑**：放大的窗口要选得能看清前后对比，这里前 30 秒、后 60 秒。

## Q3 spread 正常多宽 → 按 tick 值画计数柱状图（`d2_q3_spread.png`）
- **为什么用它**：spread 只能取 1、2、3……个 tick，是**离散**值，所以每个 tick 值画一根柱子，不要用连续 bin 的 histogram（会切出空 bin 和锯齿）。"正常"要先定义清楚：去掉开盘 10 分钟和新闻那 2 分钟。
- **讲法**："In normal trading the spread is one tick about 70% of the time, two ticks about 25%, rarely wider."
- **坑**：剔除了哪些时段一定要说出来，否则听的人会以为是全天的数据。

## Q4 典型一天的成交量 → 20 天每分钟的中位数折线，加 25–75% 分位带（`d2_q4_profile.png`）
- **为什么用它**：问的是"典型"，所以把 20 天叠在一起，每分钟取**中位数**，不用均值，避免个别异常日把曲线拉偏。再加一条 IQR 阴影带，表示日与日之间的差异有多大。这比画 20 条线（spaghetti）清楚得多。
- **讲法**："Volume is U-shaped. The first and last half hour trade three to five times the midday rate, and the band shows it's consistent day to day."
- **坑**：阴影带和中位数线用同一个颜色、浅一些，图例里说明阴影是什么。

## Q5 每天都一样吗 → 热力图：日期 × 15 分钟时段，单色系配色（`d2_q5_heatmap.png`）
- **为什么用它**：两个维度（哪一天、一天里的哪个时段）对应一个数值，正好是热力图的用法。它既能看到"每天开盘都宽"这种规律，也能一眼看到某一格的异常：11-03 的 10:00 那一格特别深。数值大小用单色系从浅到深，不要用彩虹色。
- **讲法**："Every day the spread is widest in the first 15 minutes. One cell stands out: 11-03 at 10:00, which was another news event. So today's 10:00 shock isn't unique, it's a pattern around scheduled releases."
- **坑**：一定要配 colorbar，标明单位。格子不要太多，太多就合并时段。

## Present（四句话、最多两张图：Q1 和 Q2）
> "XYZ gapped down 1.5% at 10:00 on news and recovered about half by the close. In the first 40 seconds the spread blew out from 1 to 5–10 ticks and volume ran about 15 times normal. Outside those minutes the market was normal: one-tick spreads and a U-shaped volume profile. Looking back 20 days, we've seen this kind of 10:00 widening before, so it's worth flagging scheduled news times to the desk."
