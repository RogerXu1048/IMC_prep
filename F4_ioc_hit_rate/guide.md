# F4 学习指南：IOC Hit Rate vs Latency

> 对照 `problem.md` 和 `solution.py`（120 行）一起看。运行：`python3 make_data.py && python3 solution.py`，图在 `../plots/f4_*.png`。下面所有数字都来自 `solution.py` 的实际输出。

## 0. 一句话题目
176,869 条 IOC 抢单记录（5 个 venue、2 个 strategy、20 天）：数据干不干净？哪天变了、是谁变了？速度有多重要？"S2 更好" 对不对？

## 1. 2 小时怎么分
| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify | 5 min | hit rate 定义、partial fill 怎么算 |
| Step 1 Load / clean / define | 25 min | quality counts + venue / strategy 表（95% CI） |
| Step 2 稳定性 | 25 min | 图 (a) + break 列表 + "us or them" 表 |
| Step 3 Race curve | 25 min | 图 (b) + 最快 / 最慢 decile 对比 |
| Step 4 S1 vs S2 | 20 min | by-venue 表 + standardization 数字 + 图 (c) |
| Wrap-up | 5 min | 3 条结论 + 3 条 limitations |
| Buffer / 追问 | 15 min | 200 ns 值多少钱、release 有没有用 |

## 2. 开场 3 个 clarifying questions
- "By hit rate, do you mean the fraction of our IOC attempts that get any fill, or something like a quote hit rate or our share of cleared volume?"
- "Should a partial fill count as a hit? I'd count it, since we won the race, and report filled over targeted quantity next to it."
- "Did anything change during these 20 days, like releases, venue changes or new participants, that I should know about?"

## 3. Step 1：Load、clean、define
**目标**：数清楚脏数据并逐条决定怎么处理；定义 hit；出一张带 95% CI 的表。
**先说什么**："Before any metric, I'll count duplicates and impossible latencies and decide what to do with each, because they affect different parts of the analysis."
**决策与 trade-off**
- 重复行：全列 `drop_duplicates()`（re-sent capture records），再 `assert attempt_id.is_unique`。断言失败说明有"同 id 不同内容"，那是另一类问题，不能静默丢掉。
- 缺失或 ≤0 的 `reaction_ns`：**保留整行**，只把 latency 设成 NaN。fill outcome 是真的，删行会让 hit rate 有偏；只在 race curve 里排除。
- hit = `filled_qty > 0`（partial 也算赢了 race）；quantity-based 一行：`filled_qty.sum() / target_qty.sum()`。CI 用 normal approximation（每格 n 是几百到几万）；Wilson 在 n 小或 p 接近 0/1 时更稳，n 大时两者几乎一样。

**关键代码**
```python
df = raw.drop_duplicates().copy()   # DECISION: byte-identical rows = re-sent capture records -> drop
assert df.attempt_id.is_unique      # CHECK: no "same id, different content" left behind
r = df.reaction_ns.astype("float64")  # nullable Int64 -> float, <NA> -> NaN
df["reaction_us"] = r.where(r > 0) / 1e3
df["day"] = df.date.rank(method="dense").astype(int)   # trading day 1..20 (weekends skipped)
df["hit"] = df.filled_qty > 0       # DECISION: any fill = we won the race (partials count)
```
**自检**
- rows=176,869，exact duplicates=176，reaction missing=161，reaction<=0=75。
- venue：EX1 最低 0.610（CI 0.606–0.614），EX5 最高 0.817；每个 venue 的 `qty_rate` 都比 `hit_rate` 低约 0.02（partial fills）。
- strategy：S1 0.710 < S2 0.738。先记下来，Step 4 回到这里。

## 4. Step 2：稳定性（structural breaks）
**目标**：pool 20 天之前，先确认数据稳定；找出 break，并判断是"我们变了"还是"市场变了"。
**先说什么**："Before pooling 20 days, I'll plot the daily hit rate per venue and flag day-over-day jumps that are far outside sampling noise."
**决策与 trade-off**
- 检验：两样本比例 z-test，`z = Δp / sqrt(var_d + var_{d-1})`，最简单、好解释。CUSUM 或 rolling baseline 对缓慢漂移更敏感，口头提即可。
- 阈值 |z| > 4 而不是 1.96：19 个 day pair × 5 个 venue = 95 次检验，1.96 光靠运气就会报出约 5 个 false alarm；而且同一天的 attempts 相关，z 被高估。
- "Us or them"：看 break 前后 **我方 latency** 和 **两个 strategy** 是否一起动。我方 latency 变了 → 是我们；latency 没变、两个 strategy 一起掉 → 是市场。

**关键代码**
```python
g = df.groupby(["day", "venue"]).hit.agg(["sum", "size"])
k, n = g["sum"].unstack(), g["size"].unstack()
rate, var = k / n, k / n * (1 - k / n) / n
z = rate.diff() / np.sqrt(var + var.shift())   # two-sample z-test: day d vs day d-1, per venue
breaks = pd.DataFrame({"before": rate.shift().stack(), "after": rate.stack(), "z": z.stack()})
```
**自检**：breaks = day 11 EX1 0.52 → 0.67（z 12.22）、day 11 EX2 0.72 → 0.79（z 5.13）、day 16 EX3 0.76 → 0.35（z −18.97）。
- Day 11：S1 中位 reaction 1.40 → 1.10 µs（d1-10 → d11-15），EX1 上 S1 hit 0.52 → 0.68；S2 latency 3.24 → 3.17、hit 0.06 → 0.06 没动 → **我们的 release**。
- Day 16：我方 latency 没变（EX3 上 S1 1.10 → 1.11，S2 3.23 → 3.21），但 EX3 的 S1 0.83 → 0.40、S2 0.35 → 0.02 **一起掉** → **外部原因**，最可能是更快的新竞争者。
- 因此：race curve 只用 day 1–15，S1 vs S2 只用 day 1–10。

## 5. Step 3：Race curve
**目标**：每个 venue 一条 hit rate vs 我方 reaction time 的曲线。
**先说什么**："Every attempt is a race, so the hit rate should fall as we get slower; where it starts to fall tells us how fast the competition is on each venue."
**决策与 trade-off**
- 窗口 day 1–15：EX3 的 break 会把曲线拉弯。day 11 的 release 只移动了我们在 x 轴上的位置，不改变对手，可以保留（这是假设，要说出来）。
- 每个 venue 单独做 10 个 quantile bins（`pd.qcut`）：每箱 n 相同、CI 可比。等宽分箱读起来直观，但尾部几乎没数据。
- log x 轴：venue 之间跨度大（0.84–4.42 µs），而且关心的是"慢了多少倍"。tick 用普通数字，`minorticks_off()` 去掉 `6×10⁻¹` 这种标签。

**关键代码**
```python
d = df[(df.day < NEW_REGIME_DAY) & df.reaction_us.notna()].copy()   # DECISION: stable window, pre-EX3 break
d["bin"] = d.groupby("venue").reaction_us.transform(lambda s: pd.qcut(s, n_bins, labels=False))
c = d.groupby(["venue", "bin"]).agg(us=("reaction_us", "median"), hit=("hit", "mean"), n=("hit", "size"))
ax.set_xscale("log")
ax.set_xticks([0.8, 1, 1.5, 2, 3, 4], ["0.8", "1", "1.5", "2", "3", "4"])
ax.minorticks_off()                 # otherwise log minor ticks get "6x10^-1"-style labels
```
**自检**（最快 → 最慢 decile）
- EX1：0.84 → 1.95 µs，hit 0.83 → 0.21，最先掉、最陡：竞争最快。EX5：1.23 → 4.42 µs，hit 0.92 → 0.57：竞争最慢。
- 最快一箱的 hit 是 0.83–0.92，不是 1：就算我们最快，quote 也可能已经被撤（fade）。

## 6. Step 4：S1 vs S2（Simpson's paradox）
**目标**：检验 "S2 更好"，给出一个公平的数字。
**先说什么**："Before trusting a pooled comparison, I'll check whether the two strategies trade the same venues; if not, the pooled numbers compare different opportunities, not different strategies."
**决策与 trade-off**
- 窗口 day 1–10：release 之前，两个 strategy 都没变（Step 2 的教训：不跨 break pool）。
- 公平比较：(a) 分 venue 逐个比，直观但有 5 个数；(b) **direct standardization**，两个 strategy 都用 S1 的 venue mix 加权，得到一个数；(c) 带 venue fixed effect 的回归，更一般但难解释，口头提即可。
- 用谁的 mix 是一个选择：问"S2 去做 S1 的机会会怎样"就用 S1 的；也可以用合并的 mix。说清楚即可。

**关键代码**
```python
d = df[df.day < RELEASE_DAY]        # DECISION: before the S1 release, so neither strategy changed
by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
pooled = (by_venue * mix).sum()     # = the raw pooled rate: venue rates weighted by each strategy's OWN mix
same_mix = by_venue.mul(mix.S1, axis=0).sum()
```
**自检**
- pooled：S1 0.679 < S2 0.748。但每个 venue 都是 S1 高：EX1 0.520 vs 0.060、EX3 0.818 vs 0.334、EX5 0.935 vs 0.809。
- 原因：S2 有 84.9% 的 attempts 在 EX5（最容易），S1 有 41.4% 在 EX1（最难）。
- 都放到 S1 的 mix 上：S1 0.679 vs S2 **0.218**。

## 7. 图怎么讲
- **(a) `f4_daily_hit_rate.png`**：时间序列 + CI band，是决定"能不能 pool"的第一张图。"Each line is one venue's daily hit rate with a 95% band; the day-11 jump and the day-16 cliff are far outside the bands, so I never pool across them."
- **(b) `f4_race_curve.png`**：分箱条件均值是"比例 vs 连续变量"最直观的画法。注意 EX5 前两个点之间的长直线：S1 快、S2 慢，latency 是双峰，中间没有数据，不要解读那段斜率。"Moving right means we are slower; every venue falls, and how early it falls shows how fast the competition is: EX1 first, EX5 last."
- **(c) `f4_s1_vs_s2.png`**：grouped bars 把每个 venue 和 pooled 放在一起，paradox 一眼可见。"S1 beats S2 on every venue, yet the pooled pair on the right flips, because 85% of S2's attempts sit on EX5, the easiest venue."

## 8. 结论与 limitations（英文 script，2 分钟）
> "Three conclusions. First, our hit rate is a latency race: on every venue it falls as we get slower, from 0.83 to 0.21 on EX1, the most contested venue. Second, there are two breaks. On day 11 our release made S1 faster, from 1.40 to 1.10 microseconds, and lifted EX1 and EX2. On day 16, EX3 fell from 0.76 to 0.35 for both strategies while our latency didn't move, which points to a new, faster competitor. So I never pool across those days. Third, S2's higher headline hit rate, 0.748 versus 0.679, is Simpson's paradox: 85% of S2's attempts are on EX5. Venue by venue S1 wins, and on S1's venue mix S2 would hit only 22%."
>
> "Limitations. It's observational: the slow bins are mostly S2, which chases smaller edges, so the curve isn't causal. Hit rate ignores fill size, edge and adverse selection, so more fills isn't necessarily more P&L. And competitors change, as EX3 shows, so any curve expires; with only 20 days and correlated attempts, my CIs are optimistic."

## 9. 追问速答
- **200 ns 值多少钱**：把 race curve 当 lookup table，每个 attempt 读 h(t) 和 h(t − 0.2 µs)，差值 × `target_qty` × `edge_ticks` × $1，求和后除以天数。假设：对手不反应、多出来的 fill 拿到全部 edge、曲线没被 edge / strategy 混杂。插值不外推，结果偏保守。
- **Day-11 release 有没有用**：EX1 上 S1 0.52 → 0.68，而同一 venue 的 S2 0.06 → 0.06，可以把 S2 当 control 做 diff-in-diff；还要按 venue 分层，并检查 release 前后 S1 的 venue mix 有没有变。
- **怎么做实验**：对一小部分低 edge 的 attempts 随机加 0 / 100 / 200 ns 延迟，这是测量 latency 价值最干净的方法。

## 10. 常见坑
- 删掉 latency 缺失或 ≤0 的整行：hit rate 会有偏。应该只在 race curve 里排除。
- 不先画按天的图就 pool 20 天：EX3 的 regime change 会混进 race curve。
- 看到 S2 pooled 更高就下结论；或者说得出 "Simpson's paradox" 却说不出机制（venue mix × venue 难度）。
- 把 |z| > 1.96 当 break：95 次检验加上 attempts 相关，一定会有 false alarm。
- 把 race curve 的斜率当因果效应，不提 strategy / edge 的混杂。

## 11. 练习方法
1. **第 1 轮（跟着指南）**：每个 Step 先读"先说什么"和决策，自己写代码，再对照关键代码和自检数字。
2. **第 2 轮（只看 `problem.md`）**：先写出函数名 `load / step1_hit_rates / step2_stability / step3_race_curve / step4_s1_vs_s2`，再填空。卡住超过 5 分钟才看指南。
3. **第 3 轮（模拟面试）**：空白文件，计时 2 小时，全程用英文出声；最后 2 分钟讲结论和 limitations。录音回听，检查每个 DECISION 有没有说出来。

## 12. "Hit rate" 的其他含义，分析怎么改
- **Market maker 的 quote hit rate**：分母是我们挂出的 quote，分子是被成交的次数。race 方向反过来：我们要比 taker 更快地撤单或改价。x 轴换成"标的价格跳动 → 我方 quote 更新"的延迟，y 轴换成"在 stale 状态下被成交"的比例，再用 markout 区分正常成交和被 pick off。
- **占 OCC 清算量的份额**（OCC = Options Clearing Corporation）：分子是我们的成交量，分母是全市场成交量，按 venue、标的、时段切片。份额变了可能是市场结构变了（比如 retail flow 增加），不一定是我们变了，所以同样要先查 mix，和 Step 4 是一个思路。
- 不变的部分：比例 + 诚实的 CI、先查稳定性、like-for-like 比较。"The tools are the same; what changes is the denominator and which race we're in."
