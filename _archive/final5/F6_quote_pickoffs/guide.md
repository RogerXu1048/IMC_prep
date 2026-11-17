# F6 Study Guide：报价被 pick off——markout、stale fill，以及更快 requote 值多少钱

> 用法：先读 `problem.md`，再按本指南从空文件写出 `solution.py`（170 行）。
> 每一步都按"先说（English）→ 做决策 → 写代码 → 自检 → 准备追问"的顺序走。
> 这道题对应 JD 里的 **"optimize IMC's liquidity taking and *quoting* strategies for exchange microstructure"**。IMC 的本行就是做市，这是做市一侧最典型的 latency research 问题。

## 一句话题目
"我们是期权做市商。标的一跳，我们的旧报价就过时了；在新报价生效之前被快手成交，就叫被 pick off。算出这件事每小时亏多少、为什么会发生、先修什么最划算。"

本质：**as-of join（给每笔成交找"当时的状态"）→ 按时间戳给成交分类 → 测量延迟 → 拟合经验 race curve → 做反事实估算（counterfactual）**。

## 背景知识（2 分钟就能讲清）
- **Delta**：标的涨 $1，期权 fair value 大约变动 delta 美元。call 的 delta 在 0 到 1 之间，put 在 −1 到 0 之间。
- **Stale quote**：标的跳了以后、我们的新报价生效之前，旧报价的价格已经不对了。如果新 fair 已经高过我们旧的 ask（或低过旧的 bid），对手吃掉它就能稳赚。
- **Markout**：成交之后过 h 时间再看这笔成交值多少钱，计算方式是 `sign × (fair(t+h) − price) × qty × 100`。在 h 很小时，markout ≈ 我们赚到的半个 spread；随着 h 变大，它会体现 adverse selection（被知情的对手打了）。
- **和 F4 的关系**：F4 我们是 taker，去抢别人的过期报价。F6 我们是 maker，别人来抢我们的过期报价。这是同一场比赛的两面，用的是同一个 race curve 思想。

## 2 小时时间分配

| 阶段 | 时间 | 产出 |
|---|---|---|
| Clarify + 看数据 | 5 min | 问题清单、假设 |
| Step 1 Load & audit | 15 min | 数据质量报告，quotes 排序 |
| Step 2 Markouts | 25 min | 各个 horizon 的 markout 表、P&L、图 1 |
| Step 3 Stale 分类 | 25 min | fresh vs stale 表、各 venue 分布、taker 速度 |
| Step 4 Requote latency | 30 min | latency 表、"按 series 顺序排队"的发现、race curve、图 2 |
| Step 5 What to fix | 10 min | (a) 快 2µs 和 (b) 重新排序各省多少钱、建议 |
| 收尾 | 10 min | 4–6 句结论 |

## 开场 5 分钟

**先问（English）：**
- "Are all three timestamp sources on the same clock? Is fill `ts_ns` the exchange execution time, and quote `ts_ns` the time the exchange acknowledged our new quote?"
- "Does each quote row fully replace the previous quote for that series on that venue? Do we ever pull quotes without replacing them?"
- "What should I use as fair value: our own model, or the market? Is our quote mid a good proxy for our fair?"
- "Is one hour of data representative, or was it a special session?"

**说出假设（English）：**
> "I'll assume one PTP-synced clock, that every quote row replaces the previous one on its stream, and that our quote mid is our fair value. With those, I can value fills and detect staleness purely from timestamps, without a pricing model."

---

## Step 1 — Load & audit（15 min）

**目标**：四张表读进来；fill 去重，未知 series 丢弃并计数；quotes 按时间排序；检查基本不变量。

**先说什么：**
> "First I'll audit the data. Duplicate fills would double-count P&L, and the time joins I'm about to do need sorted keys."

**决策点与 trade-offs：**
- **fill 去重的 key**：用 `fill_id`（business key），不要整行比较。交易所断线重连后可能重发执行回报，内容完全一样。
- **未知 series 的 fill（`AMD-C-140`）**：丢掉，但要报告数量。这很可能是别的 desk 的单子或者 booking 错误，要发给运营核查，不能静默吞掉。
- **quotes 不是按时间排的**（仓库里按 series 排序）：`merge_asof` 要求 `on` 键整体有序，所以**在 Step 1 排一次**，后面不要每次 join 都重新排。代价是 O(n log n) 一次，n ≈ 52 万，很快。
- **Invariant**：`bid < ask`（报价不能 crossed），ticks 按时间单调递增。用 `assert`，真实系统里这类检查会直接报警。
- **时间一律用 int64 ns**，不要转成 datetime 或 float。后面要做 µs 级的减法，int64 最安全。

**代码骨架：**
```python
fills = fills.drop_duplicates("fill_id")
unknown = ~fills.series_id.isin(series.series_id)
fills = fills[~unknown]
quotes = quotes.sort_values("ts_ns", kind="stable").reset_index(drop=True)
assert (quotes.bid < quotes.ask).all()
series["rank"] = series.groupby("underlying").cumcount()      # 在 series_id 顺序里的位置（Step 4 要用）
fills = fills.merge(series[["series_id", "underlying", "delta"]], on="series_id").sort_values("ts_ns")
fills["sign"] = np.where(fills.side == "B", 1, -1)
```

**自检**：
- 重复 fill **190** 条，未知 series **15** 条，`quotes_time_sorted=False`。
- 清洗后 ticks 4,332 条、quotes 520,200 条、fills 38,086 条，共 237,766 张合约。

**可能的追问：**
- *"Why not drop_duplicates on all columns?"* 如果重发的回报里有某个字段不同（比如接收时间），整行比较会漏掉。业务主键才是正确的去重依据。
- *"What if quotes didn't fit in memory?"* 按 underlying 分片，每个分片内部有序，join 也只在同一个 underlying 内部做，天然可以并行。

---

## Step 2 — Markouts（25 min）

**目标**：对每笔 fill，算出 h ∈ {1µs, 10µs, 100µs, 1ms, 10ms, 100ms, 1s} 时的 markout（$/合约），以及这一小时的 P&L。

**先说什么：**
> "Markout is the standard way to value a market maker's fill: compare the fill price with fair value a little later. I'll use our own quote mid as fair, looked up with an as-of join at t + h."

**决策点与 trade-offs：**
- **拿什么当 fair**：
  - **我们自己的 quote mid**：不需要定价模型，数据里现成就有，而且就是我们当时认为的 fair。缺点是继承了我们自己模型的误差。
  - **交易所 NBBO mid**：独立于我们的模型，但期权 spread 很宽、噪声大，而且这里没给这份数据。
  - **delta × 标的变动**：需要额外的 reference fair，还忽略了 gamma。
  - **结论**：用自己的 mid，同时说出它的局限。
- **as-of join 还是循环**：`pd.merge_asof(by=["series_id","venue"])` 一次就能完成所有 fill 的查找，复杂度 O(n log n)。手写循环要对每个 fill 二分查找，代码多，而且慢。
- **按合约加权还是按 fill 平均**：P&L 是按合约算的，所以用 `Σ(mo × qty) / Σqty`。按 fill 简单平均会让 1 张的小单和 50 张的大单权重一样。

**代码骨架：**
```python
def our_mid_at(fills, quotes, t_ns):
    q = quotes[["ts_ns", *STREAM]].assign(mid=(quotes.bid + quotes.ask) / 2)
    left = fills[["fill_id", *STREAM]].assign(ts_ns=t_ns.to_numpy())    # fills 已按时间排好
    return pd.merge_asof(left, q, on="ts_ns", by=STREAM, direction="backward").mid.to_numpy()

for h in HORIZONS_US:
    fills[f"mo_{h}"] = fills.sign * (our_mid_at(fills, quotes, fills.ts_ns + h * 1000) - fills.price) * MULT
```

**自检**：
- 平均 markout（$/合约）：1µs **1.93**，10µs **1.50**，100µs **0.79**，之后基本平稳，1s 时 **0.70**。
- 这一小时 1s markout 下的 P&L 是 **$167,414**。
- 1µs 时接近 $2，也就是半个 spread（0.02 × 100），这是对的：这时候还没人改价。
- 在 10–100µs 之间掉得很厉害，说明有一部分成交在几十 µs 内就变成亏损。这是 stale fill 的信号，引出 Step 3。

**可能的追问：**
- *"Why does the curve drop between 1 and 100 µs, then go flat?"* 在 µs 级别能让 fair 改变的，只有"我们自己马上要改价"这一件事。这说明这些成交发生在我们改价之前，正是 stale 的定义。
- *"Which horizon would you report to the desk?"* 报多个。短 horizon 看 latency 问题，长 horizon（秒、分钟）看 informed flow 或 alpha 问题。

---

## Step 3 — 哪些 fill 是 stale（25 min）

**目标**：只用时间戳判断每笔 fill 是 fresh 还是 stale，并汇总笔数、合约数、P&L、venue 分布和 taker 的反应速度。

**先说什么：**
> "A fill is stale if the underlying moved after our live quote was set but before the fill. I can test that with two as-of joins: the last underlying tick before the fill, and our last quote before the fill."

**决策点与 trade-offs：**
- **用时间戳规则还是价格规则**：
  - 时间戳规则：`tick_ts > quote_ts`，不依赖模型，简单，可以解释。
  - 价格规则：比较成交价和新 fair，需要模型，阈值也难定。
  - **结论**：先用时间戳规则；价格信息留给 Step 4 衡量"值多少钱"。
- **规则在什么情况下会错**（主动说出来，面试官很喜欢）：
  - 时钟不同步：PTP 误差和 µs 级窗口是同一个量级时就危险了。
  - 一个完全无信息的客户恰好在 stale 窗口里成交，会被误判为 stale。
  - 两次 tick 间隔很近，导致 requote 和 tick 对不上。
  - 价格只动了很小一点、并没有"穿过"我们报价的 tick，也会被算成 stale。
- **taker 反应时间**：`fill_ts − tick_ts`，它就是对手的速度分布。

**代码骨架：**
```python
t = ticks[["ts_ns", "underlying"]].rename(columns={"ts_ns": "tick_ts"})
q = quotes[["ts_ns", *STREAM]].rename(columns={"ts_ns": "quote_ts"})
f = pd.merge_asof(fills, t, left_on="ts_ns", right_on="tick_ts", by="underlying")
f = pd.merge_asof(f, q, left_on="ts_ns", right_on="quote_ts", by=STREAM)
f["stale"] = f.tick_ts > f.quote_ts
f["taker_us"] = (f.ts_ns - f.tick_ts) / 1e3
f["pnl_1s"] = f.mo_1000000 * f.qty
f.groupby("stale").agg(fills=("fill_id", "size"), contracts=("qty", "sum"), pnl_1s=("pnl_1s", "sum"))
```

**自检：**

| | fills | contracts | P&L @1s | $/合约 |
|---|---|---|---|---|
| fresh | 35,974 | 177,058 | +$338,304 | +1.91 |
| stale | 2,112（5.5%） | 60,708（25.5%） | **−$170,890** | −2.81 |

- stale 的笔数只占 5.5%，但亏掉了 fresh 赚到的钱的**一半**。原因是 taker 会把我们挂出的全部 size 一次吃光，所以按合约算占到了 25.5%。
- 按 venue：EX1 620、EX2 676、**EX3 816** 笔，EX3 最多。
- taker 反应时间中位数 **8.4µs**。
- 和 `_truth.parquet` 对比，分类 100% 一致。真实数据里不会这么干净，原因见上面"规则在什么情况下会错"。

**可能的追问：**
- *"Fresh fills also decay from 2.00 to 1.91 at 1 s — same problem?"* 不是。那是秒级的 informed flow，属于 alpha 或定价问题，处理方法是调宽 spread、根据 flow 来源区别对待（tiering）。stale 是 µs 级的 latency 问题，处理方法是更快地改价。要分开处理，也要分开汇报。

---

## Step 4 — Requote latency 和 race curve（30 min）

**目标**：
1. 每次 requote 的延迟 = quote 生效时间 − 触发它的 tick 时间。按 venue 看分布，找出延迟和什么有关。
2. 算出 race curve：P(被 pick off | 存在可吃的价差, 我们的延迟)。

**先说什么：**
> "Now the why. For every requote I'll find the tick that triggered it and measure our latency. Then I'll look at which quotes were actually exposed, meaning our new fair moved through the old quote, and estimate how often an exposure gets picked off as a function of our latency."

**决策点与 trade-offs：**
- **触发 tick 怎么找**：用 as-of backward join（quote 时间之前的最后一个 tick，同一个 underlying）。如果两次 tick 间隔小于一个 batch，会配错；这里很少见，但要说出来。
- **延迟和什么有关**：
  - 先按 venue 看，会发现 EX3 慢约 4.5µs，这是路径（网络）问题。
  - **再按 series 在 series_id 顺序中的位置看**：延迟随位置线性上升，每个 series 0.5µs，第 40 个比第 1 个慢约 20µs。说明引擎是**一个 series 接一个 series 串行**发 requote 的。这是软件里的排队问题，比网络差异大得多。
  - **注意别画循环论证的图**：如果按"生效时间的先后"给 quote 排名，再画延迟 vs 名次，必然是递增的，什么也说明不了。要用**外生的**顺序，这里是 series_id 的排序。
- **Exposure 的定义**：新 mid 高过旧 ask，或低过旧 bid（`through > 0`）。只有这种情况对手吃了才赚钱。只动 1 个 tick 的小跳，乘上 delta 以后通常小于半个 spread，不构成 exposure。
- **Race curve 用经验分箱还是参数模型**：先用经验分箱（2µs 一档），简单、不需要假设分布形状，代价是每一档的样本有噪声。时间更多的话，可以拟合 `p_hunt × LogNormalCDF(L)`。

**代码骨架：**
```python
q = quotes.sort_values([*STREAM, "ts_ns"])
prev = q.groupby(STREAM)[["bid", "ask"]].shift()
mid = (q.bid + q.ask) / 2
q = q.assign(through=np.maximum(mid - prev.ask, prev.bid - mid))
q = q.merge(series[["series_id", "underlying", "delta", "rank"]], on="series_id").sort_values("ts_ns")
q = pd.merge_asof(q, t, left_on="ts_ns", right_on="tick_ts", by="underlying").dropna(subset=["tick_ts"])
q["lat_us"] = (q.ts_ns - q.tick_ts) / 1e3
slope = {v: np.polyfit(g["rank"], g.lat_us, 1)[0] for v, g in q.groupby("venue")}

exposed = q[q.through > 0].copy()
hit = fills[fills.stale].groupby([*STREAM, "tick_ts"]).size().rename("n_hit").reset_index()
exposed = exposed.merge(hit, on=[*STREAM, "tick_ts"], how="left")
exposed["picked"] = exposed.n_hit.fillna(0) > 0
curve = exposed.groupby((exposed.lat_us // 2) * 2).picked.mean()
```

**自检：**
- requote latency p50/p99（µs）：EX1 **12.5 / 22.5**，EX2 **13.6 / 23.6**，EX3 **17.2 / 27.1**。
- 延迟随 series 位置的斜率：三个 venue 都是 **0.5 µs/series**。
- exposure 共 **65,559** 次，其中 **3.22%** 被 pick off。
- race curve：4µs 以下几乎是 0，6–10µs 快速上升，16µs 以上稳定在约 **5%**。
- **怎么读这条曲线**：上升段对应 taker 的速度分布，中位数约 8–9µs，和 Step 3 的 8.4µs 吻合。平台高度对应"有人来狩猎的概率"，约 5%。到了平台以后再慢也不会更糟，因为该来的人已经来了。

**可能的追问：**
- *"Is EX3's extra loss a network or software problem?"* 两者都有。EX3 的路径基础延迟慢约 4.5µs，但所有 venue 都受串行 batch 影响，最多多出 20µs。先修影响更大的那个。
- *"Why not just parallelize the batch?"* 可以：多线程，或者用交易所的 mass quote 一次发出多个改价。但会受交易所限流和 message rate 收费的限制，工程成本也高。排序几乎是免费的，见 Step 5。

---

## Step 5 — 先修什么（10 min）

**目标**：用 race curve 估算两个方案每小时各省多少钱，并给出建议。

**先说什么：**
> "I'll turn the curve into an expected loss per exposure, which is P(picked | latency) × through × size × 100, and check that the model reproduces the realised loss. Then I'll replay two counterfactuals: the whole path 2 µs faster, and the same hardware with requotes sent in a better order."

**决策点与 trade-offs：**
- **先校验模型**：模型给出的总亏损应该接近实际亏损。模型 $163,881/h，实际 $167,137/h，吻合，才可以拿它做反事实估算。
- **(a) 整条路径快 2µs**：把每个 exposure 的延迟减 2µs，用 `np.interp` 在曲线上查出新的概率。
- **(b) 按 |delta| × size 降序排队**：
  - 每个 batch 里，"第 k 个位置"的延迟大致固定；改变的只是哪个 series 坐在哪个位置。
  - 要最小化 `Σ wᵢ · P(Lᵢ)`，其中 P 随 L 递增。根据**排序不等式（rearrangement inequality）**，把权重最大的 series 放到最早的位置就是最优解。
  - 权重 = |delta| × size，因为同一个 batch 里所有 series 面对的是同一次 tick 跳动，暴露大小只差在 |delta| × size。
  - 引擎在发 requote 之前就知道这些数，所以完全可以实现。
- **反事实的假设**（一定要说出来）：
  - 对手的行为不变（实际上对手可能会适应）；
  - 每个位置的延迟只由位置决定；
  - 曲线在新的延迟区间仍然成立（不外推太远）。

**代码骨架：**
```python
def expected_loss(exposed, lat_us, curve):
    p = np.interp(lat_us, curve.index + 1, curve.to_numpy())
    return float((p * exposed.through * exposed["size"] * MULT).sum())

base = expected_loss(exposed, exposed.lat_us, curve)
faster = expected_loss(exposed, (exposed.lat_us - 2).clip(lower=0), curve)
w = exposed.groupby(["underlying", "venue", "series_id"]).agg(
    w=("delta", lambda d: abs(d.iloc[0])), size=("size", "first"), rank=("rank", "first"))
w["new_rank"] = (w.w * w["size"]).groupby(level=[0, 1]).rank(ascending=False, method="first") - 1
# new latency = old latency + slope * (new_rank - rank)
```

**自检：**
- 实际亏损 **$167,137/h**，模型 **$163,881/h**。
- (a) 快 2µs 省 **$16,449/h**；(b) 重新排序省 **$57,047/h**，约是 (a) 的 3.5 倍，而且不用改硬件。

**可能的追问：**
- *"How would you validate (b) before a full rollout?"*
  - 先在一个 venue 或一个 underlying 上做 canary，对照组保持原来的顺序；
  - 比较 stale fill 的比例、markout 和 race curve；
  - 跑足够多个小时，看差异是否稳定。
- *"What else could reduce pick-offs?"*
  - 标的一动就先**撤**价，之后再挂新价，但这会受撤单限流约束；
  - 用交易所的 mass quote / mass cancel；
  - 开启 market maker protection；
  - 在不确定的时候临时调宽高 delta series 的 spread。

---

## 收尾（English script）
> "Over one hour, stale fills were only 5.5% of fills but 25% of contracts, and they gave back about half of the $338k we earned on fresh fills. The cause is mostly self-inflicted. After each underlying move our engine sends requotes one series at a time in series_id order, at 0.5 µs per series, so the last series in a batch waits about 20 µs. Fast takers react in about 8 µs. EX3 also has a path that is about 4.5 µs slower. Sending requotes in order of exposure, |delta| times size, is optimal by the rearrangement inequality and saves about $57k an hour without new hardware. That's roughly three times what a 2 µs network upgrade would save, so I'd do the reordering first, validate it with a canary on one venue, and then look at parallelising the batch and EX3's path."

**再多给时间会做的：**
- 给 race curve 拟合参数模型，并给出置信区间；
- 用多天的数据验证稳定性；
- 把 fresh fill 在秒级的 markout 衰减（informed flow）单独拿出来分析；
- 研究撤单和 mass quote 的方案，以及交易所的 throttle 限制。

## 常见坑
1. 忘了把 quotes 按时间排序，`merge_asof` 直接报错；或者每次 join 都重新排一遍，浪费时间。
2. 去重用整行比较，或者忘记去重，P&L 被重复计算。
3. markout 用按 fill 的简单平均，而不是按合约加权。
4. 画"延迟 vs 生效先后名次"的图，那是循环论证。要用外生的 series_id 顺序。
5. 把所有 tick 都当作 exposure。小跳乘上 delta 往往小于半个 spread，没人会来吃。
6. 用模型做反事实之前，没有先校验模型能复现实际亏损。
7. 结论只说"要更快"，没有比较不同修法的收益和成本。面试官想听到的是一个**取舍**。

## 练习方法
- **Round 1**：跟着本 guide 一步一步写，每个 `# DECISION` 都用英文说一遍。
- **Round 2**：只看 `problem.md` 和函数名（`step1_load_and_audit`、`our_mid_at`、`step2_markouts`、`step3_classify_stale`、`step4_requote_latency`、`expected_loss`、`step5_whats_it_worth`），自己写。
- **Round 3**：空白文件，2 小时计时，全程用英文出声讲，最后用上面那段收尾词收尾。
