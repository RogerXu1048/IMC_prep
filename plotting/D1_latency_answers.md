# D1 答案：Gateway latency

参考代码：`D1_latency_solution.py`。图在 `plots/d1_q*.png`。

## Q1 整体分布 → histogram，x 轴取 log（`d1_q1_hist.png`）
- **为什么用它**：问的是"长什么样"，也就是一个变量的分布，所以用 histogram。latency 是右偏、长尾的；线性坐标下只能看到一根尖峰，尾巴被压扁了，所以换成 log x。再画上 p50 和 p99 两条线，让数字和形状对应起来。
- **不用什么**：不要只给一个 mean，也不要用 bar chart 画平均值。均值会被尾巴拉高，而且什么形状都看不出来。
- **讲法**："Latency is right-skewed, so I plot it on a log axis. The median is about 9 µs but p99 is about 190 µs, so the tail is what we should care about. There's also a small bump around 150–300 µs, which I'll look into next."
- **坑**：bin 要用 `np.logspace` 等比切分。在 log 轴上用等宽 bin，形状会失真。

## Q2 比较 4 个 gateway → box plot 加 ECDF（`d1_q2_compare.png`）
- **为什么用它**：比较几组分布时，box plot 能一眼看到中位数和分散程度。我把 whisker 设成 p1–p99，这样尾巴的长度就能比。但 box 只给汇总数字，看不出**形状**。ECDF（累积分布）能看到完整形状：gw-3 的曲线在 0.95 附近出现第二个台阶，说明有约 5% 的订单走了一条慢路径（双峰）。
- **不用什么**：不要画 4 条重叠的 histogram，会糊成一片。小提琴图也可以，但 ECDF 能直接读出分位数，更精确。
- **讲法**："The medians are identical, so this isn't a general slowdown. The ECDF shows that gw-3 has a second step: about 5% of its orders take 150–300 µs. That looks like a separate slow code path, not just noise."
- **坑**：ECDF 的 x 轴也要用 log。颜色要固定对应到 gateway，后面所有图都一样。

## Q3 一天里有没有变化 → 按时间画折线，p50 和 p99 分成上下两个 panel，突出 gw-2（`d1_q3_over_time.png`）
- **为什么用它**：问题带着"随时间"，所以用 line plot。先按 5 分钟分桶算分位数，因为逐笔数据太密。p50 和 p99 量级不同，所以分成两个 panel 共用 x 轴，**不用双 y 轴**。要讲的是 gw-2，所以把它画成蓝色，其他画成灰色（emphasis）。
- **讲法**："Medians are flat all day, apart from the open. But gw-2's p99 climbs steadily through the afternoon, from about 20 µs to over 100 µs. A tail that grows with time while the median stays flat often points to something like a leak or a growing queue."
- **坑**：只看 p50 会完全漏掉这个问题。gw-3 的 p99 全天都高，那是 Q2 里的慢模式，不是新问题，要说明一下。

## Q4 为什么某些时刻慢 → hexbin，叠加分桶的 median 和 p90（`d1_q4_vs_load.png`）
- **为什么用它**：两个连续变量之间的关系，通常用 scatter。但这里有 20 万个点，scatter 会严重 overplot，所以用 hexbin（2D 密度），再按 load 分桶，叠加 median 和 p90 两条线，趋势就清楚了。
- **讲法**："Latency is flat up to about 60 msgs/ms, then rises steeply. That's a classic queueing knee. The open and the close are when load crosses that line, which explains the spikes in Q3's median."
- **坑**：样本少的桶不要画，分位数会乱跳。y 轴用 log。高处那条 150–300 µs 的带子是 gw-3 的慢模式，和 load 无关。

## Present（给 manager 看的那张）
选 Q3（emphasis 版）或 Q2 的 ECDF，取决于你想先讲哪件事。标题直接写结论，例如 "gw-2's p99 climbs all afternoon while its median stays flat"。
> "Two issues. gw-2's tail degrades through the afternoon, so we should look at memory or queue growth. And about 5% of gw-3's orders take a separate slow path. Everything else is explained by load: above about 60 msgs/ms we hit a queueing knee."
