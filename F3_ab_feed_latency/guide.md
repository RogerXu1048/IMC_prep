# F3 Study Guide：A/B Feed Latency，B 线升级到底有没有用？
> 先读 `problem.md`，再跟着本 guide 写 `solution.py`（120 行）。下面所有数字都来自 `python3 solution.py` 的输出（seed 固定，约 3 秒跑完）。

## 0. 一句话题目 + 2 小时怎么分
**一句话**：同一份交易所行情从 A、B 两条线各收一遍，抓了两天；网络团队说 B 线交换机升级后更快了。先审数据，再看延迟能不能信，最后用"同一个包"的对比回答升级有没有用，并找出 load 的 knee。

| 时间 | 做什么 | 产出 |
|---|---|---|
| 0–5 min | Clarify + 说 4 步计划 | 3 个问题、假设 |
| 5–30 | Step 1 load + audit | rows/dups/ooo 表、missing 表、两条线都丢的 seq |
| 30–55 | Step 2 每条线的延迟 | p50/p99 + drift 表、CCDF 图 |
| 55–95 | Step 3 升级有没有用 | naive 表、paired 表、按 load 分桶的柱状图 |
| 95–110 | Step 4 load effect | latency vs load 图、knee |
| 110–120 | 收尾 | 2 分钟结论 + limitations，留 buffer |

**核心思路**：两条线运的是**同一批包**，而且都用我们自己的接收时钟。同一个包的 B − A 能把流量、交易所和时钟三方面的差异全部抵消。

**Clarifying questions（英文）：**
- "Is `seq` contiguous per day, so a seq missing on both lines was really lost upstream?"
- "Are both capture NICs hardware-timestamped from the same PTP-disciplined clock?"
- "By 'faster', do we care about the median, the tail, or how often B wins the race?"

## 1. Step 1：Load + audit
**目标**：每条线、每天数 rows / duplicates / out-of-order / missing；找两条线**都**丢的 seq；判断丢包是随机还是成簇。

**先说什么**："Before any latency number, I want to know what's wrong with the raw data: duplicates, reordering and gaps, per line and per day."

**决策与 trade-off：**
- Duplicate 保留第一份：第二份 rx 更晚、没有新信息，留着会拉高延迟。乱序用 `cummax`：文件按到达顺序排，seq 小于之前见过的最大 seq 就是乱序到达（先排除 duplicate 本身）。
- 缺号用排好序后相邻 seq 的 gap（`diff() - 1`）：一个 gap = 一段连续丢包（run）。`mean_run ≈ 1` 是随机丢，`> 1` 是成簇。Trade-off：一天最开头、最末尾的丢包看不到。
- 两条线都丢的 seq = 1..max 里 A、B 都没有的数。这是**正确性**问题（book 会错），只能靠 retransmission 或 snapshot 恢复。

**关键代码：**
```python
df["ooo"] = (df["seq"] < df.groupby(["line", "date"])["seq"].cummax()) & ~df["dup"]
df = df.loc[~df["dup"], ["line", "date", "seq", "exch_ns", "rx_ns"]]    # DECISION: keep 1st copy
s = df.sort_values(["line", "date", "seq"])
s["gap"] = s.groupby(["line", "date"])["seq"].diff() - 1      # seqs missing just before this one
audit = s[s["gap"] > 0].groupby(["line", "date"])["gap"].agg(missing="sum", runs="size").astype(int)
audit["mean_run"] = audit["missing"] / audit["runs"]           # ~1 = random, >1 = clustered
both = df.groupby("date")["seq"].max() - df.drop_duplicates(["date", "seq"]).groupby("date").size()
only_a = (df.groupby(["date", "seq"])["line"].transform("size") == 1) & (df["line"] == "A")
```

**自检：**
- A 在 2026-11-11 有 **25** 个 duplicate；B 在 2026-11-10 有 **15** 个乱序；其余都是 0。
- Missing：A 是 159 / 262 个（**0.054% / 0.055%**，mean_run 1.046 / 1.074，随机）；B 是 60 / 1018 个（**0.021% → 0.213%**，mean_run 1.714 / 1.529，成簇）。
- 两条线都丢：2026-11-10 **10** 个，2026-11-11 **22** 个，共 32 个。
- B 丢掉的那些 seq，在 A 上的 median latency 是 **253 us**（全体 25 us）：B 是在 microburst 里丢包的。

## 2. Step 2：每条线、每天的延迟
**目标**：p50/p99 表；一张能看到尾部的图；论证绝对 one-way latency 不可信，但同一个包的 A vs B 可信。

**先说什么**："rx minus exch mixes two clocks, so before trusting it I'll check whether its floor moves the same way on both lines."

**决策与 trade-off：**
- 报 p50/p99 不报 mean：latency 重尾，mean 会被少数慢包拉走。
- 每分钟取 **minimum**：去掉排队，只剩"物理路径 + clock offset"。两条独立的线漂得一样，漂的就是时钟，不是网络。（假设每分钟都有包，所以用 `range(len(m))` 当 x。）
- 同一个包的 A vs B 仍然有效：exch_ns 相同，rx 都是我们的时钟，offset 直接抵消。

**关键代码：**
```python
pct = df.groupby(["line", "date"])["lat_us"].agg(p50="median", p99=lambda s: s.quantile(0.99))
mins = df.groupby(["line", "date", df["exch_ns"] // 60_000_000_000])["lat_us"].min()
pct["min_drift_us_per_h"] = mins.groupby(level=[0, 1]).agg(lambda m: 60 * np.polyfit(range(len(m)), m, 1)[0])
x = np.sort(g["lat_us"].to_numpy())
ax.plot(x, 100 * (1 - np.arange(len(x)) / len(x)), c=COLOR[line], ls=DASH[date], label=f"{line} {date}")
```

**自检：**
- p50 / p99（us）：A 11-10 **24.60 / 65.70**；A 11-11 **25.22 / 124.21**；B 11-10 **27.42 / 69.65**；B 11-11 **24.02 / 110.67**。
- 每分钟最小值的漂移：**1.00、1.00、1.02、1.01 us/h**，四组一样，是时钟漂移（2 小时约 2 us）。

## 3. Step 3：升级到底有没有用？
**目标**：先做 naive before/after，找到 confounder（load），再做 paired B − A（按天、按 load bucket），报告 B 先到的比例。

**先说什么**："The naive answer compares B's p99 across the two days, but if day two is busier that's confounded, so I'll use A as a control and compare B with A on the same packet."

**决策与 trade-off：**
- Load 用两条线的**并集**算：只用 B 自己收到的包，B 在 burst 里丢的包就不算进去，正好低估 B 最忙的时候。`np.searchsorted` 在排好序的 exch_ns 上做二分查找，向量化，几十万行不到 1 秒。
- **Paired**：同一个包的 B − A，再按 load 分桶，确认结论在每个负载水平都成立。比"按 load 分层比较 B 的两天"更强，因为流量组合和时钟也抵消了。
- Inner join：int64 ns 保持精确（outer join 有 NaN，epoch ns 会变成 float64，精度丢到 256 ns）。代价是 B 丢的包不在里面（survivor bias）。

**关键代码：**
```python
u = df.drop_duplicates(["date", "seq"]).sort_values("exch_ns")
u["load"] = np.arange(len(u)) - np.searchsorted(u["exch_ns"], u["exch_ns"] - WINDOW_NS, side="right") + 1
naive["busy_pct"] = 100 * (u["load"] >= 100).groupby(u["date"]).mean()    # the confounder
p = a.merge(b[["date", "seq", "exch_ns", "rx_ns"]], on=["date", "seq"], suffixes=("", "_B"))
assert (p["exch_ns"] == p["exch_ns_B"]).all()          # CHECK: identical packets on both lines
p["b_minus_a_us"] = (p["rx_ns_B"] - p["rx_ns"]) / 1e3
paired = p.groupby("date")["b_minus_a_us"].agg(median="median", b_first_pct=lambda s: 100 * (s < 0).mean())
by_load = p.groupby(["bucket", "date"], observed=True)["b_minus_a_us"].median().unstack()
```

**自检：**
- Naive p99：B **69.6 → 110.7 us**，看起来变差了；但没动过的 A 也 **65.7 → 124.2 us**。
- busy_pct（load ≥ 100 msgs/100us 的消息占比）：**1.7% → 5.1%**，11-11 的 burst 约多了 3 倍。
- Paired median B − A：**+2.52 → −1.20 us**（B 相对 A 改善约 3.7 us）；B 先到的比例 **1.07% → 95.00%**。
- 按 load bucket：11-10 每档都是正的（**+2.30 到 +4.64 us**），11-11 每档都是 **−1.14 到 −1.22 us**。

## 4. Step 4：Load effect
**目标**：latency vs load，找出排队开始的 knee。

**先说什么**："Next I'll plot latency against load, because it tells the network team whether the tail is a switch problem or a capacity problem."

**决策与 trade-off：**
- 画 median 不画 p99：高负载的 bin 样本少，p99 很噪。10 msgs 一个 bin，160 以上合并成最后一个 bin。
- Knee 用一条写死的规则："第一个 median 比自己 idle（load < 10）高 3 us 以上的 bin"。阈值是人为选的，所以报一个范围，不报精确值。

**关键代码：**
```python
load_bin = df["load"].clip(upper=160) // 10 * 10                    # last bin = 160+
curve = df.groupby(["line", "date", load_bin])["lat_us"].median().unstack(["line", "date"])
knee = (curve > curve.iloc[0] + 3).idxmax()
```

**自检**：knee 是 A **80 / 80**，B **70 / 80** msgs/100us，结论写 "~70–80"。

## 5. 图怎么讲
- **`f3_latency_tail.png`（CCDF，log-log）**：比较 4 个重尾分布、重点看尾部，直方图在线性轴上看不到尾巴；颜色 = 线，虚线 = 升级前，实线 = 升级后。"In the tail, both solid lines sit far right of the dashed ones, line A included, so B's own before/after tail change can't be blamed on the switch."
- **`f3_paired_by_load.png`（柱状图，0 为基准）**：直接回答问题，比的是同一批包上带正负号的差值，并且控制了 load。"Every grey bar is above zero and every orange bar is below it, so on identical packets B went from slower to faster at every load level."
- **`f3_latency_vs_load.png`（折线，阴影 = knee）**：load 是有序变量，用折线；4 条线叠在一起说明 knee 跟线路无关。"All four curves stay flat until about 70 to 80 messages per 100 microseconds and then climb together, so the tail is driven by load, not by the switch."

## 6. 结论与 limitations（English script，2 分钟）
> "Yes, the upgrade helped. On the same packets, line B went from about 2.5 microseconds slower than A to 1.2 microseconds faster, in every load bucket, and B now arrives first on 95% of packets, up from 1%. The scary p99 increase on B, from 70 to 111 microseconds, isn't the switch: the second day had about three times as many messages in bursts, and line A, which nobody touched, got worse too. Both lines start queueing at about 70 to 80 messages per 100 microseconds, and B drops packets inside those bursts, so the tail is a capacity question. Separately, 32 sequence numbers were lost on both lines, so our retransmission path has to stay healthy.
>
> Three limitations. First, it's only two days and load is measured, not controlled, so I can't rule out other overnight changes. Second, absolute one-way latency includes a clock offset that drifts about one microsecond per hour, so I only trust same-packet differences. Third, the paired comparison drops the packets B lost, which were B's slowest, so it flatters B a little."

## 7. 常见坑
1. 只比较 B 的 before/after p99，不用 A 当 control、不看 load，就说"升级让尾部变差了"。
2. 把 `rx − exch` 当成真实物理延迟，不提 clock offset 和 drift。
3. 乱序检测时没排除 duplicate，或者干脆不 dedupe，duplicate 会把延迟拉高。
4. Load 只用 B 自己收到的包算，B 在 burst 里丢的包没计入，load 被低估。
5. 用 outer join 合并后 epoch ns 变成 float64，精度丢到 256 ns，出现假平局。

## 8. 练习方法（三轮）
- **Round 1：跟着 guide 写。** 每一步先大声说"先说什么"，再敲关键代码，跑通后对照"自检"的数字。
- **Round 2：只看 `problem.md` 和函数名**（`load`、`step1_audit`、`step2_latency`、`step3_upgrade`、`step4_load_effect`、`main`），空白文件自己写，卡住超过 5 分钟再看对应那一节。
- **Round 3：空白文件，计时 2 小时，全程英文 talk aloud。** 每一步写之前先说 decision，最后 2 分钟用第 6 节的结构讲结论 + limitations，录音回听。
