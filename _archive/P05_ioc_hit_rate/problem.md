# P05 — IOC Hit Rate vs Latency: What Is a Nanosecond Worth?

**Time box: ~60 min** (Parts A–C are the core; D–F are stretch / discussion)

## Context

We trade US equity options on five exchanges (EX1–EX5). Our two liquidity-taking strategies, **S1** and **S2**, watch market data. When a resting quote is mispriced relative to our theoretical value, they send an **IOC (immediate-or-cancel)** order to take it. Other firms see the same opportunity at the same moment, so every attempt is a **race**: whoever reaches the exchange's matching engine first gets the fill. Sometimes the quote also disappears before anyone arrives (the market maker cancels it, a "fade").

The latency team wants to know:

- How often do we win (our **hit rate**)? Where do we win and lose?
- How does the hit rate depend on our **reaction time**?
- What would it be worth if the engineers made our reaction **200 ns faster**?
- Did last month's software release actually help?

> In this problem **hit rate = the fraction of take attempts that receive a fill**. The term is ambiguous in practice (count of attempts vs. filled quantity, partial fills, etc.). In a real interview, clarify the definition first, and say which one you chose and why.

## Data

Run `python3 make_data.py` first. It writes:

`../data/p05/take_attempts.parquet` (~175k rows): one row per IOC take attempt over 20 trading days starting 2026-10-19.

| column | type | meaning |
|---|---|---|
| `attempt_id` | int64 | unique id of the attempt |
| `date` | datetime | trading date |
| `ts_ns` | int64 | UTC epoch nanoseconds when the opportunity was detected |
| `venue` | str | exchange, `EX1` … `EX5` |
| `strategy` | str | `S1` or `S2` |
| `underlying` | str | underlying ticker of the option |
| `edge_ticks` | int64 | theoretical edge of the opportunity, in ticks (how mispriced the quote was) |
| `spread_ticks` | int64 | bid-ask spread at detection time, in ticks |
| `target_qty` | int64 | displayed size we tried to take (contracts) |
| `reaction_ns` | Int64 (nullable) | our reaction time: signal arrives at our NIC → our order leaves our NIC (hardware timestamps) |
| `resp_ns` | int64 | time from our order leaving the NIC until the exchange's response arrived |
| `filled_qty` | int64 | contracts filled (0 … target_qty) |
| `outcome` | str | `FILLED`, `PARTIAL`, or `MISSED` |

Assume a tick is $0.01 and the contract multiplier is 100 (so 1 tick of edge on 1 contract = $1).

## Tasks

### Part A — Load, clean, define, summarize (≈10 min)
1. Load the data. Check data quality: duplicates, missing or impossible values, internal consistency between columns. Decide (and say) what you do with each problem.
2. Compute the hit rate two ways: **count-based** (attempts with any fill) and **quantity-based** (filled contracts / targeted contracts). When would you prefer each?
3. Produce a table of hit rate **overall, by venue, and by strategy**, with a 95% confidence interval.
4. Plot the **daily hit rate per venue**. Look at it carefully and describe anything noteworthy.

### Part B — Hit rate vs reaction time (≈10 min)
1. For each venue, bin attempts by `reaction_ns` (choose a sensible binning) and compute the hit rate per bin with a confidence interval.
2. Plot hit rate vs reaction time, one line per venue. Describe the shape. Which data should you include/exclude here, and why?

### Part C — S1 vs S2 (≈10 min)
A trader says: *"S2 must be our better-engineered strategy — its overall hit rate is higher than S1's."*
1. Check the claim. Compare the strategies overall and within each venue.
2. Explain what is going on, and produce one number that compares the strategies fairly.

### Part D — Modeling: what is latency worth? (stretch, ≈15 min)
1. Fit a **logistic regression** of hit on reaction time, venue, and edge (implement it yourself with numpy/scipy — no sklearn). Interpret the reaction-time coefficient.
2. Think about the mechanism: we win if we arrive before the fastest competitor and the quote hasn't faded. Write down a model of P(hit | our latency t) in terms of the competitor-latency distribution. Fit it per venue (choose a parametric family and justify it). What does it say about the competition on each venue?
3. Estimate the value of being **200 ns faster**: extra fills per day and approximate $ per day. State every assumption.

### Part E — Did the release help? (stretch, ≈10 min)
S1 had a software release that took effect on **2026-11-02** (S2's code did not change).
1. Estimate the release's effect on S1's hit rate. Start naive, then improve the estimate. What could bias a simple before/after comparison?
2. Which time window and which comparison group would you use, and why? Give an uncertainty estimate.

### Part F — Communicate (≈5 min)
Write a short paragraph and make **one chart** for the trading desk summarizing what you found and what you recommend.

## Follow-up questions the interviewer may ask
1. Why use a Wilson interval instead of `p ± 1.96·sqrt(p(1−p)/n)`? When does it matter?
2. Are attempts independent? What does that mean for your confidence intervals, and how would you fix it?
3. Your logistic model is linear in reaction time on the logit scale. Is that the right functional form? How would you check?
4. How could `edge_ticks` confound the latency effect? In which direction?
5. If we get 200 ns faster, will the gain persist? What will competitors do?
6. Can you infer the competitors' latency distribution from our own data? What assumptions does that require?
7. How would you design an experiment (instead of an observational study) to measure the value of latency?
8. Hit rate went up but P&L went down — how could that happen? (adverse selection / winner's curse)
9. How would you set up monitoring so that a sudden change on one venue is caught on the day it happens?

## 考点（中文）
- 指标定义：hit rate 的口径（按次数 vs 按数量、partial fill 怎么算），先澄清再计算
- 数据清洗与 EDA 直觉：重复记录、缺失/负的 latency、按天按 venue 画图发现异常
- 统计推断：binomial 置信区间（Wilson）、分箱、Simpson's paradox、分层/标准化比较
- 建模：自己实现 logistic regression；把"抢单"机制写成概率模型并做 distribution fitting（MLE）
- 因果推断：before/after 的混杂因素、diff-in-diff、选窗口、按天 block bootstrap
- 沟通：把结论和建议讲给 trader 听（一段话 + 一张图）
