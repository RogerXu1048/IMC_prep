# P06 — Fill-Probability Model: Evaluation, Leakage, and Distribution Shift

**Time box: ~60 min** (Parts A–C are the core; D–F are stretch / discussion)

## Context

This problem reuses the IOC take-attempt data from P05. (You do not need to have solved P05.)

Trading wants a model of **P(fill)** that can be evaluated **at decision time**, before we send an IOC. The strategy would then send an IOC only when it is worth it in expectation:

> send IOC  ⟺  P(fill) · edge  >  expected cost (fees, risk, and the information we leak by trying)

So the model has to **rank** opportunities and also produce **calibrated probabilities**. You are asked to build a simple model and, more importantly, to **evaluate it honestly** and decide whether it can be trusted going forward.

Rules of the game: no sklearn is installed. Use pandas / numpy / scipy, and implement the model and the metrics yourself (they are short). The interviewer can help with syntax.

## Data

Run `python3 make_data.py` first. It generates P05's data if it is missing.

`../data/p05/take_attempts.parquet` (~175k rows): one row per IOC take attempt over 20 trading days starting 2026-10-19.

| column | type | meaning |
|---|---|---|
| `attempt_id` | int64 | unique id of the attempt |
| `date` | datetime | trading date |
| `ts_ns` | int64 | UTC epoch nanoseconds when the opportunity was detected |
| `venue` | str | exchange, `EX1` … `EX5` |
| `strategy` | str | `S1` or `S2` |
| `underlying` | str | underlying ticker |
| `edge_ticks` | int64 | theoretical edge of the opportunity, in ticks |
| `spread_ticks` | int64 | bid-ask spread at detection time, in ticks |
| `target_qty` | int64 | displayed size we tried to take |
| `reaction_ns` | Int64 (nullable) | our reaction time: signal at our NIC → our order leaves our NIC |
| `resp_ns` | int64 | time from our order leaving the NIC until the exchange's response arrived |
| `filled_qty` | int64 | contracts filled |
| `outcome` | str | `FILLED`, `PARTIAL`, or `MISSED` |

The exchange is in New York; US daylight saving time ended on 2026-11-01.

## Tasks

### Part A — Target, features, leakage (≈10 min)
1. Define the target variable.
2. Go through the columns and classify each as: usable at decision time / not usable / borderline. Justify the borderline ones.
3. Some column(s) would make the model look excellent. Find them, quantify how much they inflate the evaluation, and explain why they must be excluded.

### Part B — How should we split the data? (≈10 min)
1. Train and evaluate the same model with (a) a random 70/30 split over all days, and (b) a **time-based** split: train on days 1–10, validate on days 11–13, test on days 14–20.
2. Compare the numbers. Which one would you report to Trading, and why?

### Part C — Models and metrics (≈15 min)
1. Build at least: a constant baseline, a per-venue base-rate table, and a **logistic regression** (your own implementation; think about feature transforms, standardization, categorical encoding, interactions).
2. Implement and report **log loss, Brier score, AUC** (rank-based), and **expected calibration error (ECE)** on validation and test.
3. Draw a **reliability diagram** on the test set. Is the model calibrated everywhere?

### Part D — Has the world changed? (stretch, ≈15 min)
1. Compare the feature distributions in train vs test (e.g. **PSI** and **KS**). Which features shifted, and is each shift a problem for the model?
2. Run **adversarial validation**: train a classifier to distinguish the training period from a later period. Interpret its AUC. Try more than one pair of periods.
3. Break model performance and calibration down **by day and by venue**. Locate any degradation as precisely as you can and classify it: covariate shift, label shift, or concept drift?

### Part E — Remedies (stretch, ≈10 min)
Assume labels arrive with a one-day delay. Evaluate on days 18–20, using whatever labels would be available by then.
1. Compare: the static model; the static model + **per-venue Platt recalibration**; a **rolling-window retrain**; a **recency-weighted** retrain.
2. Which would you deploy, and what monitoring would you add?

### Part F — Communicate (≈5 min)
In a few bullet points, tell Trading what the model **can** and **cannot** be trusted for right now.

## Follow-up questions the interviewer may ask
1. What is the difference between covariate shift, label (prior) shift, and concept drift? Give a trading example of each.
2. How can AUC stay roughly the same while calibration breaks? Which one matters for the "send iff P·edge > cost" rule?
3. Why must standardization statistics (mean/std) be computed on the training set only?
4. The fill rate here is ~70%. What changes if it were 2%? (class imbalance, metrics, sampling)
5. How often would you retrain? What is the tradeoff of a short vs long training window?
6. How do you monitor a model when labels arrive late or never?
7. Is `reaction_ns` a legitimate feature? What would you use in production?
8. A feature's PSI is huge. Should you drop the feature?
9. How would you test that the training/evaluation pipeline itself is correct (no leakage, reproducible)?

## 考点（中文）
- Leakage 识别：哪些字段是 outcome 之后才知道的（"too good to be true" 的 AUC）
- 正确的 train/valid/test 划分：time-based split vs random split，不能用未来预测过去
- 自己实现 logistic regression 和各种指标（log loss、Brier、AUC、ECE、reliability diagram）
- Distribution shift：PSI / KS / adversarial validation；区分 covariate shift、label shift、concept drift
- 补救与上线：recalibration（Platt）、rolling retrain、recency weighting、监控与报警
- 沟通：告诉 trader 模型在哪些地方可信、哪些地方不可信
