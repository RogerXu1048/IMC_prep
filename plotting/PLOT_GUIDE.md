# Plot Guide：交易和性能分析里选什么图、怎么讲

面试反馈的重点：**怎么拆解数据、怎么把它讲给别人听**，比挖出多少 insight 更重要；不需要复杂算法；要熟悉最基本的图，以及每种图什么时候用；**解释方法和理由**比提改进建议更重要。

**怎么练**：
1. 先读完这一页。
2. 每组练习（D1–D4，各约 30 分钟）先只看 `*_drill.md`，自己选图、画图、出声讲。
3. 再对照 `*_answers.md` 和 `plots/` 里的参考图。

数据用 `python3 make_data.py` 生成。

| 练习 | 场景 | 练到的图 |
|---|---|---|
| [D1](D1_latency_drill.md) | gateway latency | histogram（log x）、box plot、ECDF、按时间的折线（small multiples + emphasis）、hexbin + 分桶线 |
| [D2](D2_market_drill.md) | 一天的行情 | 折线 + 事件标注、step 图 + 共用 x 轴的 panel、离散计数柱状图、分位带、热力图（日期 × 时段） |
| [D3](D3_execution_drill.md) | 各 venue 执行质量 | 点图 + CI、散点 + 分桶趋势线、热力图（venue × 时段）、100% 堆叠面积图 |
| [D4](D4_pnl_drill.md) | 策略 PnL | 累计 PnL 折线、回撤图、排序横向柱状图、相关系数热力图、before/after 对比 |

## 1. 讲一张图：每次都走这 4 步
1. **问题**："The question is whether … "（先说你要回答什么）
2. **为什么选这种图**："Because this is a distribution / trend / relationship / comparison, I'll use … rather than … "
3. **图上看到什么**：先说坐标轴和单位，再说最主要的形状，最后说例外。"The x-axis is …; most orders sit around …; the exception is … "
4. **注意事项**：样本量、数据怎么过滤的、相关不等于因果。"One caveat: … "

**拆解数据的顺序**（从粗到细，每一步一张图）：
整体分布 → 按一个维度切开（gateway、venue、strategy）→ 随时间看 → 和另一个变量的关系 → 盯住最异常的那一组，放大看。

## 2. 选图速查表

| 你要回答的问题 | 首选 | 交易 / 性能里的例子 | 别用 / 注意 |
|---|---|---|---|
| 一个变量**长什么样**（分布） | **Histogram** | latency、每日 PnL、滑点 | 右偏数据 x 轴取 **log**，bin 用 `logspace`；离散值（spread 的 tick 数）每个值画一根柱 |
| 比较几组**分布**，看尾部 | **ECDF / CCDF**（x 轴 log） | 各 gateway 或 A/B 线路的 latency | 直接读出 p50/p99；能看出双峰 |
| 比较**很多组**分布的汇总 | **Box plot**（或 violin） | 每个 venue、每天的 latency | box 看不出双峰；whisker 的定义要说清 |
| **随时间**的变化 | **Line plot** | 价格、每分钟 p99、成交量 | 先 resample；放大的窗口和全天分开画 |
| 分段常数的时间序列 | **Step plot**（`where="post"`） | bid/ask、仓位、限额 | 普通折线会画出从来不存在的斜线 |
| 累积的结果 | **累计折线**，加 **回撤图** | 策略 PnL | 每日柱子叠在一起没法读 |
| 一条"典型曲线"和它的波动范围 | **中位数线 + 分位带**（`fill_between`） | 日内成交量、spread 的 profile | 不要画 20 条 spaghetti 线 |
| 两个连续变量的**关系** | **Scatter**；点很多时用 **hexbin**，再叠加分桶的 median/p90 | latency vs load、滑点 vs 订单大小 | 数据量大时抽样、调透明度；画出 knee |
| 类别之间**比大小** | **排好序的 bar**（从 0 开始）；差别小时用 **点图 + CI** | 每个 venue 的成交率、每个 symbol 的 PnL | 标出 n 和 CI；名字长就用横向；类别多就合并成 "Other" |
| 组成占比随时间变化 | **100% stacked area / bar** | 路由份额、成交量按 venue 的构成 | 不用饼图；只有最底层能精确读数 |
| 两个维度对应一个数值 | **Heatmap**（单色系） | 日期 × 时段、venue × 时段 | 选指标时让"颜色深 = 糟"；配 colorbar |
| 正负都有的矩阵 | **Heatmap**（发散配色，0 居中） | 策略之间的相关系数 | 格子少时直接写数值 |
| 一个事件前后的对比 | **时间序列 + 事件竖线**，加 **前后两段的 box/ECDF**，标 n | deploy、交换机升级 | 说出有多大把握（样本量、噪声） |
| 分布是否符合某个模型 | **QQ plot** | latency 是不是 lognormal | 尾部偏离直线的部分最重要 |
| 两个单位不同的量 | **上下两个 panel 共用 x 轴** | 价格和成交量 | **永远不要用双 y 轴** |

## 3. 画图 checklist（每张图交出去之前过一遍）
1. **标题直接写结论**，例如 "gw-2's p99 climbs all afternoon"，而不是 "Latency over time"。
2. 坐标轴写名字**和单位**（µs、bps、$k、%）。
3. 偏态数据用 log 轴；柱状图的 y 轴从 0 开始；折线不必从 0 开始。
4. 颜色**固定跟着实体**（gw-2 在所有图里都是同一个颜色）；不超过 4–5 种颜色，更多就用 emphasis（一个高亮、其余灰色）或者拆成多个小图。
5. 4 条以内的线直接在线尾写名字，比图例好读。
6. 小样本要标 n，比例要画置信区间。
7. 数据过滤了什么（去掉开盘、去掉某个 venue）写在标题或注释里。
8. 一张图只讲一件事，讲不完就再画一张。

## 4. 常见坑
- 用均值代表重尾分布，用线性轴画 latency。
- 20 万个点直接 scatter，overplot 成一团黑。
- 双 y 轴（两个刻度可以随意缩放，看起来"相关"的趋势可能是假的）。
- 柱状图的 y 轴截断，夸大差距。
- 饼图：类别一多就读不了，也画不了负数。
- 先按组汇总再比较，忽略各组构成不同（Simpson's paradox）。
- 盘中图把隔夜时间画进去，出现一条很长的"直线"。

## 5. 常用英文句式
- "I'll start with the overall distribution, then break it down by gateway, then look at it over time."
- "I'm using a histogram on a log axis because latency is heavily right-skewed."
- "A box plot summarizes it, but the ECDF shows the full shape, and here the shape matters."
- "I put volume in a separate panel with a shared time axis rather than a second y-axis."
- "There are 200,000 points, so a scatter would overplot. I'll use a hexbin with binned medians."
- "This venue looks worst, but it only has 169 orders, so the confidence interval overlaps the others."
- "The takeaway is in the title: the median is flat, but the tail grows through the afternoon."
- "One caveat: this is correlation. To confirm the cause I'd want to …"

## 6. matplotlib 最小代码（面试时可以请面试官帮忙补语法）
```python
fig, ax = plt.subplots(1, 2, figsize=(11, 4))                              # 1 行 2 列
ax.hist(x, bins=np.logspace(0, 3, 60)); ax.set_xscale("log")               # log histogram
ax.plot(np.sort(x), np.arange(1, len(x) + 1) / len(x))                     # ECDF
ax.boxplot([a, b, c], tick_labels=["A", "B", "C"], whis=(1, 99), showfliers=False)
s.resample("5min").quantile(0.99).plot(ax=ax)                              # 按时间的分位数（需要 DatetimeIndex）
ax.step(t, bid, where="post")                                              # 报价
ax.fill_between(x, lo, hi, alpha=0.2); ax.plot(x, mid)                     # 分位带
ax.hexbin(x, y, gridsize=60, bins="log", cmap="Blues")                     # 大数据量散点
ax.errorbar(rate, names, xerr=ci, fmt="o")                                 # 点图 + CI
ax.barh(names, values)                                                     # 排序横向柱状图（先 sort）
ax.stackplot(dates, shares.T, labels=cols)                                 # 堆叠面积
im = ax.imshow(grid, cmap="Blues", aspect="auto"); fig.colorbar(im)        # 热力图（相关系数用 cmap="RdBu_r", vmin=-1, vmax=1）
fig, ax = plt.subplots(2, 1, sharex=True)                                  # 上下 panel 共用 x 轴
ax.axvline(t0, color="k", lw=0.8); ax.annotate("deploy", (t0, y))          # 标事件
ax.set(title="takeaway", xlabel="... (unit)", ylabel="... (unit)")
```
