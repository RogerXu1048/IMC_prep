# F4 学习指南：IOC Hit Rate vs Latency

> 对照 `problem.md`（题面）和 `solution.py`（参考解，168 行）一起看。运行：`python3 make_data.py && python3 solution.py`，图会输出到 `../plots/f4_*.png`。
> 本指南里的所有数字都来自 `solution.py` 的实际输出。

## 0. 一句话题目 + 时间分配

**一句话**：给你 17 万次 IOC 抢单记录，回答四个问题：我们在哪里赢、速度有多重要、两个策略谁更好、快 200 ns 值多少钱。

| 阶段 | 时间 | 产出 |
|---|---|---|
| 开场澄清 | 5 min | 定义 hit rate，说出假设 |
| Step 1 清洗 + 定义 + 表 | 20 min | data-quality 计数，venue/strategy 两张表（Wilson CI） |
| Step 2 按天稳定性 | 20 min | 每日 hit rate 图，z-test 找出的 break 列表 |
| Step 3 race curve | 25 min | hit rate vs reaction time 分箱图 |
| Step 4 S1 vs S2 | 20 min | Simpson's paradox 的解释，标准化后的对比数字 |
| Step 5 200 ns 的价值 | 20 min | 每个 venue 每天多的 fills 和 $，加上假设清单 |
| 收尾 | 10 min | 3–5 句结论，以及"有更多时间我会做什么" |

**这道题考什么**：数据分析部分是分组比例、置信区间、分箱、分层比较。算法部分是 Wilson 公式、day-over-day z-test、direct standardization、经验曲线插值。面试官最看重的是 **你选哪个时间窗口、为什么**，以及 **你能不能识破 Simpson's paradox**。

---

## 1. 开场 5 分钟：先澄清，再说假设

**要问的问题（英文）**
- "When you say hit rate, do you mean the fraction of our take attempts that get any fill, the filled quantity over targeted quantity, or something like our share of total market volume?"
- "How should partial fills count? I'd count them as a hit, because we won the race."
- "Is `reaction_ns` measured with hardware timestamps on the same clock at both ends? Can it be missing or negative?"
- "Has anything changed during these 20 days — releases, new venues, exchange changes — that I should know about?"
- "Is the goal a number for an engineering investment decision, or a monitoring tool?"

**要说的假设（英文）**
> "I'll use a count-based hit rate as the main metric and report the quantity-based fill rate next to it. I'll treat each attempt as one Bernoulli trial for the confidence intervals, knowing that this understates uncertainty because attempts within a day are correlated. And before anything else, I'll check whether the data is stable over time, because a regime change would contaminate every downstream number."

---

## 2. Step 1：Load、clean、define

**目标**：把数据读进来，数清楚有哪些脏数据并决定怎么处理，定义 `hit`，输出两张带 CI 的表。

**先说什么**
> "First I'll load the data and count the obvious quality issues: duplicates, missing latency, and impossible latency. Then I'll define the metric and build a summary table with confidence intervals."

**决策点与 trade-offs**

| 决策 | 选项 A | 选项 B | 选哪个，为什么 |
|---|---|---|---|
| 重复行 | 全列去重 `drop_duplicates()` | 按 `attempt_id` 去重 | 先全列去重，再 `assert attempt_id.is_unique`。如果断言失败，说明有"同 id 内容不同"的情况，那是另一类问题，不能静默丢掉 |
| 缺失或负的 latency | 整行删除 | 保留行，但打 `latency_ok=False` | **选 B**。outcome 是真实的，删掉会让 hit rate 产生偏差；只在用到 reaction time 的分析里排除这些行 |
| hit 定义 | count-based（有成交就算 hit） | quantity-based（filled/target） | 主指标用 count：它衡量我们是否赢了 race。quantity-based 一起报，因为它更接近 P&L |
| CI | Wald `p ± 1.96·sqrt(p(1−p)/n)` | Wilson | **Wilson**。不会超出 [0,1]；在 n 小或 p 接近 0/1 时覆盖率仍接近 95%。这里按天 × venue 的 cell 只有几百个样本 |

**代码骨架**
```python
def wilson_ci(k, n, z=1.96):
    k, n = np.asarray(k, float), np.asarray(n, float)
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return centre - half, centre + half

df = raw.drop_duplicates().copy()
assert df.attempt_id.is_unique
df["day"] = df.date.rank(method="dense").astype(int)
df["hit"] = df.filled_qty > 0
df["latency_ok"] = (df.reaction_ns.notna() & (df.reaction_ns > 0)).fillna(False).astype(bool)

g = df.groupby("venue").agg(n=("hit", "size"), k=("hit", "sum"),
                            target=("target_qty", "sum"), filled=("filled_qty", "sum"))
g["hit_rate"] = g.k / g.n
g["ci_lo"], g["ci_hi"] = wilson_ci(g.k, g.n)
```

**自检**
- 应该看到：raw 176,869 行，重复 176 行，`reaction_missing`=161，`reaction_nonpositive`=75。
- venue 表：EX1 0.610 最低，EX5 0.817 最高；各 venue 的 `qty_rate` 都比 `hit_rate` 低约 2 个百分点（partial fill 的影响）。
- strategy 表：S1 0.710，S2 0.738。**S2 比 S1 高**，先记下来，Step 4 会回到这里。
- CI 很窄（±0.004 左右），因为 n 是几万。

**可能的追问**
- *Wilson 的直觉？* 它是对 score test 反解出来的区间，中心向 0.5 收缩，所以在极端 p 时不会塌缩成宽度 0。n 很大时它和 Wald 几乎一样。
- *为什么用 `rank(method="dense")` 而不是 `date.dt.day`？* 交易日不连续（有周末），dense rank 给出 1..20 的交易日序号，后面按天比较和切窗口更方便。

---

## 3. Step 2：按天看是否稳定（找 structural break）

**目标**：画每个 venue 的每日 hit rate，再用一个简单检验自动标出变化太大、不像噪声的日子。

**先说什么**
> "Before pooling 20 days together, I want to see whether the process is stationary. I'll plot the daily hit rate per venue and flag day-over-day changes that are far larger than sampling noise."

**决策点与 trade-offs**
- **检验方法**：两样本比例的 z-test，`z = (p_d − p_{d−1}) / sqrt(var_d + var_{d−1})`。
  - 替代方案 1：CUSUM。对缓慢漂移更敏感，但要多调参数。
  - 替代方案 2：用前 N 天的均值做基线。对单日的噪声更稳。
  - 面试里先写最简单的 z-test，再口头提这两个。
- **阈值取 5，而不是 2**：attempts 不独立（同一天的市场状况会影响所有 attempts），真实方差比 binomial 方差大，z 会被高估。用高阈值是务实的做法。严格的做法是 day-block bootstrap。

**代码骨架**
```python
daily = df.groupby(["day", "venue"]).agg(n=("hit", "size"), k=("hit", "sum"))
rate = (daily.k / daily.n).unstack()            # rows = day, cols = venue
var = rate * (1 - rate) / daily.n.unstack()
z = rate.diff() / np.sqrt(var + var.shift())
breaks = z.stack()[lambda s: s.abs() > 5]
```
画图：每个 venue 一条线，加 `fill_between` 画 Wilson CI，x 轴标日期。

**自检**（参考解输出）

| day | venue | before | after | z |
|---|---|---|---|---|
| 11 | EX1 | 0.519 | 0.672 | +12.2 |
| 11 | EX2 | 0.720 | 0.790 | +5.1 |
| 16 | EX3 | 0.763 | 0.352 | −19.0 |

**怎么解读（这是本题最关键的一步）**
- **Day 11（11-02）**：EX1、EX2 跳升。去查 S1 的 reaction time：从这天起中位数大约快了 300 ns，所以这是一次 **我方 release**。
- **Day 16（11-09）**：只有 EX3 暴跌，**两个 strategy 都跌**，我方 latency 没变，其他 venue 也没变。这是 **外部原因**：最可能是 EX3 上出现了一个更快的新竞争者。
- **决定**：后面估计 race curve 只用 **days 1–15**（在 `STABLE_DAYS` 里写死），否则 EX3 的 regime change 会把曲线拉弯。
> "Day 16 on EX3 is a regime change that we didn't cause. I'll exclude it from the latency analysis and flag it to the desk separately."

**可能的追问**
- *怎么区分"我们变了"和"市场变了"？* 同时监控我方 latency 分布。hit rate 掉了、我方 latency 没变，那就是外部原因；我方 latency 变了，那就是内部原因。
- *day 11–15 市场也更波动（fade 增多），会影响什么？* 会让 release 的 before/after 比较低估效果，需要用 S2 做 control（diff-in-diff），见"有更多时间"一节。

---

## 4. Step 3：Race curve（hit rate vs reaction time）

**目标**：对每个 venue，把 attempts 按我方 reaction time 分箱，画出 hit rate 随速度变化的曲线。

**先说什么**
> "Every attempt is a race, so the hit rate should fall as we get slower. The shape of that fall tells us how fast the competition is on each venue. I'll use quantile bins per venue on the stable window."

**决策点与 trade-offs**
- **分箱方式**：用 quantile bins（`pd.qcut`），每箱样本数相同，CI 宽度可比。等宽分箱读起来直观，但尾部箱子几乎是空的，CI 很宽，容易被噪声误导。
- **每个 venue 单独分箱**：各 venue 的 latency 分布不同，混在一起分箱会让某个 venue 的大部分样本挤在一两个箱里。
- **数据窗口**：days 1–15，并且 `latency_ok`。
- **已知的混杂**：慢的那几箱里大多是 S2（中位 3.2 µs），而 S2 的 edge 更小、竞争更弱，所以曲线尾部被抬高（偏乐观）。要说出来；严格的做法是再按 edge 分层。

**代码骨架**
```python
d = df[df.day.isin(STABLE_DAYS) & df.latency_ok].copy()
d["bin"] = d.groupby("venue").reaction_us.transform(lambda s: pd.qcut(s, 10, labels=False))
curve = d.groupby(["venue", "bin"]).agg(t_us=("reaction_us", "median"),
                                        n=("hit", "size"), k=("hit", "sum"))
curve["rate"] = curve.k / curve.n
curve["lo"], curve["hi"] = wilson_ci(curve.k, curve.n)
ends = curve.groupby("venue").rate.agg(["first", "last"])
assert (ends["first"] > ends["last"]).all()      # slower -> lower hit rate
```
画图：`ax.errorbar(c.t_us, c.rate, yerr=...)`，x 轴用 log scale。

**自检**
- EX1 从最快一箱的 0.83 降到最慢一箱的 0.21，**最陡**，说明竞争最快。
- EX5 从 0.92 降到 0.57，曲线最靠右，说明竞争最慢。
- 曲线左端是一个 plateau（约 0.85–0.92），它是 `1 − fade`：就算我们最快，quote 也可能已经被撤了。
- EX5 的点在 1.2 µs 和 2.2 µs 之间有一段空白。原因是 EX5 上 S1（快）和 S2（慢）的 latency 是双峰分布，中间没有样本。

**可能的追问 / 加分点**
- *能从这条曲线推出竞争者的 latency 分布吗？* 可以。我们赢 ⟺ 我们比最快的竞争者先到，且 quote 没 fade。所以 `h(t) = (1 − fade) · P(C > t)`，也就是说 **`h(t)/(1−fade)` 就是竞争者 latency 的 survival function**。我们自己的 latency 有自然波动，相当于在不同的 t 上采样了这条曲线。前提假设：我方 latency 和竞争者 latency 独立、fade 和 latency 无关、各箱之间其他条件可比。
- *为什么用 bin median 而不是 bin 中点作为 x？* 箱内分布偏斜，median 更能代表箱里的典型 attempt。

---

## 5. Step 4：S1 vs S2（Simpson's paradox）

**目标**：检验"S2 更好"这个说法，并给出一个公平的比较。

**先说什么**
> "Before I trust an aggregate comparison, I want to check whether the two strategies trade the same mix of venues. If they don't, the overall number compares different opportunities, not different strategies."

**决策点与 trade-offs**
- **窗口**：只用 days 1–10，也就是 release 之前、两个策略都没有改过代码的时期。
- **公平比较的方法**：
  - (a) 分 venue 逐个比较。最直观，但得到的是 5 个数。
  - (b) **Direct standardization**：把两个策略都放在同一个 venue mix（S1 的）上加权。得到一个数，面试官要的就是这个。
  - (c) 回归加 venue 固定效应。最一般，但代码更多，也更难解释。

**代码骨架**
```python
d = df[df.day <= 10]
by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
standardized = by_venue.mul(mix["S1"], axis=0).sum()
```

**自检**（参考解输出）
- 整体：S1 0.679 < S2 **0.748**。
- 但 **每个 venue 都是 S1 高**：EX1 0.520 vs 0.060、EX3 0.818 vs 0.334、EX5 0.935 vs 0.809。
- 原因是 venue mix：S2 有 **84.9%** 的单在 EX5（竞争最慢的 venue），S1 有 41.4% 在 EX1（竞争最快的 venue）。
- 速度：S1 中位 1.40 µs，S2 中位 3.21 µs。
- 标准化到 S1 的 venue mix 后：S1 0.679 vs **S2 0.218**。

> "S2's overall hit rate is higher only because 85% of its attempts are on EX5, where the competition is slow. Within every venue S1 wins more, which makes sense since it is more than twice as fast. On the same opportunities S2 would hit about 22% versus S1's 68%."

**可能的追问**
- *那 S2 是不是应该关掉？* 不一定。S2 选择在 EX5 这样竞争慢的地方交易，可能正是合理的分工。hit rate 也不等于 P&L，要看每笔的 edge 和 markout。
- *标准化应该用谁的 mix？* 取决于问题：问"如果 S2 去做 S1 的机会会怎样"就用 S1 的 mix；也可以用两者合并的 mix。说清楚选择即可。

---

## 6. Step 5：快 200 ns 值多少钱

**目标**：用 Step 3 的经验曲线，估计每个 attempt 快 200 ns 后的成交概率变化，汇总成每天多出的 fills 和 $。

**先说什么**
> "I'll use the empirical race curve as a lookup table. For each attempt, I read the hit probability at its current reaction time and at 200 nanoseconds less, then sum the differences, weighted by size and edge. No parametric model — just interpolation."

**决策点与 trade-offs**
- **非参数插值 vs 参数模型**：
  - 插值：简单透明，不需要假设分布形状。但 `np.interp` 在观测范围外取端点值、不外推，所以最快那批 attempts 拿不到任何收益，结果偏 **保守**。
  - 参数模型（logistic、lognormal 竞争者模型）：可以外推，但依赖函数形式。原版 P05 用结构模型算出大约 +460 fills/天、$11.8k/天，比这里的插值高约 15%，这正好体现了这个 trade-off。
- **用哪些 attempts 来定价**：days 11–15。它们是 release 之后的 **当前 latency 分布**，又在 EX3 变化之前。曲线本身用 days 1–15 估计。
- **$ 的口径**：`extra_fills × target_qty × 平均成交比例 × edge_ticks × $1`（一个 tick 0.01 × 乘数 100 = $1 每张合约）。

**代码骨架**
```python
d = df[df.day.between(11, 15) & df.latency_ok].copy()
p_now, p_fast = np.zeros(len(d)), np.zeros(len(d))
for v, c in curve.groupby(level="venue"):
    m = (d.venue == v).to_numpy()
    t = d.reaction_us.to_numpy()[m]
    p_now[m] = np.interp(t, c.t_us, c.rate)
    p_fast[m] = np.interp(t - 0.2, c.t_us, c.rate)
d["extra_fills"] = p_fast - p_now
d["extra_usd"] = d.extra_fills * d.target_qty * fill_frac * d.edge_ticks * 1.0
per_day = d.groupby("venue")[["extra_fills", "extra_usd"]].sum() / d.day.nunique()
```

**自检**（参考解输出）
- TOTAL：大约 **+398 fills/天、$10,154/天**。
- 其中 EX1 占大头：+270 fills、$7,325。原因是 EX1 的曲线最陡，而且 S1 一半的单都在 EX1。
- EX3、EX4 几乎为 0：我方 latency 落在这两个 venue 曲线的 plateau 上，快 200 ns 也没什么用。

**必须说出的假设**
1. 竞争者不反应（实际上是 arms race，优势会被侵蚀）；
2. 多出来的 fill 能拿到全部 theoretical edge，没有 adverse selection；
3. 曲线没有被 edge 或 strategy 的 mix 混杂；
4. 未来的机会结构和 days 11–15 相同。

> "So roughly 400 extra fills and ten thousand dollars a day, concentrated on EX1. Treat it as an upper bound on edge capture and a lower bound on the curve extrapolation. If the engineering cost is small relative to that, it's worth doing — but I'd confirm it with a small randomized-delay experiment first."

---

## 7. 收尾（10 min）

**结论怎么讲（英文，4–6 句）**
> "Our hit rate is determined by how our reaction time compares with the fastest competitor on each venue: EX1 is the most contested and EX5 the least. Two things changed during the period: the S1 release on Nov 2 raised hit rates on EX1 and EX2, and from Nov 9 EX3 collapsed from 76% to 35% for both strategies with no change on our side, which looks like a new, faster competitor. S2's higher headline hit rate is a venue-mix effect; venue by venue S1 is much better. Being 200 ns faster is worth roughly 400 fills and $10k of theoretical edge per day, mostly on EX1. I'd prioritise latency work for EX1, and ask the desk to review sizing on EX3."

**有更多时间我会做的事**
- **Logistic regression**：`hit ~ reaction + venue + edge`，自己用 numpy 写 IRLS，用来控制 edge 的混杂。
- **结构模型 + distribution fitting**：用 MLE 拟合 `(1−fade)·P(C>t)`，C 取 lognormal，从而估计每个 venue 竞争者的 latency 中位数（原版 P05 估出约 1.5 / 2.2 / 2.8 / 3.6 / 5.1 µs）。
- **Release 的因果效应**：naive before/after 低估了效果。原因一是 release 之后 S1 把更多单放到了更难的 EX1，二是 days 11–15 市场更难。做法是先按 venue 分层，再用 S2 做 control 做 **diff-in-diff**，置信区间用 day-block bootstrap。
- **Isotonic regression (PAV)**：让经验曲线单调递减，减少插值噪声。
- **随机延迟实验**：对一小部分低 edge 的 attempts 随机加 0/100/200 ns 的延迟。这是测量 latency 价值最干净的方法。
- **Markout P&L**：看成交后 1s、10s 的 mid 变化，检查多赢的那些单是不是被 adverse selection 了。

---

## 8. "Hit rate" 的另外两种解读，以及分析怎么改

1. **做市商的 quote hit rate**：我们挂出去的报价中被成交的比例，或者成交时报价是否已经过时（被 pick off）。
   - 分母换成"我方 quote 的存续区间"；分子换成"被成交的次数"。
   - race 的方向反过来：我们要比 taker **更快撤单或更新报价**。所以 race curve 的 x 轴变成"标的价格跳动 → 我方报价更新"的延迟，y 轴是"报价在过时状态下被成交的比例"。
   - 同样要用 Wilson CI、按天找 break、按 venue 分层，再加上 markout 来区分好的成交和被 pick off 的成交。
2. **占 OCC 清算量的份额**：OCC = Options Clearing Corporation，美国所有上市期权都在这里清算。
   - 分母是全市场成交量（OCC 或 OPRA 的公开数据），分子是我们的成交量。按 venue、标的、时段切片。
   - 份额的变化可能来自市场结构的变化（比如散户订单流在增加），不一定是我们的竞争力变了。所以同样要检查 **mix 的变化**，这和 Step 4 的思路完全相同。

**面试里的话术**："The tools are the same — proportions with honest CIs, stability over time, and like-for-like comparisons. What changes is the denominator and which race we're in."

---

## 9. 常见坑
- 把缺失或负 latency 的行整行删掉，导致 hit rate 有偏。正确做法是只在 latency 分析里排除。
- 不先画按天的图，直接用 20 天汇总。这样 EX3 的 regime change 会混进 race curve 和 200 ns 的估值里。
- 用等宽分箱且不看每箱样本量，尾部只有几十个点却画得很"确定"。
- 看到 S2 整体更高就下结论，或者知道 Simpson's paradox 却说不出 **为什么**（venue mix × venue 难度）。
- `pd.qcut` 遇到 NaN 或负数会出问题，要先过滤 `latency_ok`。
- 算 $ 时不说假设，或者忘了 partial fill（要乘以平均成交比例）。
- 把 z > 2 当作显著。attempts 不独立，实际方差比 binomial 方差大。
- 把 race curve 的斜率直接当作因果效应，没提 edge 和 strategy 的混杂。

## 10. 练习方法
1. **第 1 轮（跟着指南）**：每个 Step 先读"先说什么"和决策点，自己写代码，再对照骨架和自检数字。每一步跑通再进入下一步。
2. **第 2 轮（只看题面）**：只打开 `problem.md`，自己先写下 5 个函数名（`step1_load_and_define` … `step5_value_of_speed`），然后填空。卡住超过 5 分钟才看指南。
3. **第 3 轮（模拟面试）**：空白文件，计时 2 小时，**全程用英文出声说**：每一步先说 what / why，写完说你检查了什么。录音回听，看自己有没有说出每个 DECISION 和 TRADE-OFF。
