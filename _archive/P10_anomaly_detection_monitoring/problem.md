# P10 — Anomaly Detection on Trading-Server Monitoring Metrics

**Time box: ~60 min** · pandas + numpy + matplotlib (scipy allowed)

## Context

Our market-data and order-gateway servers publish one row of health metrics per minute. On-call engineers get paged when something "looks wrong", but the current alerting is a fixed threshold on p99 latency and it pages constantly at the market open, so people have started ignoring it.

You've been asked to build a better detector, show that it really is better (with numbers, not anecdotes), and report anything else unusual you notice in the data. The audience is the infrastructure team and the head of trading operations.

## Data

Run `python3 make_data.py` first. Files are in `data/p10/`.

**`metrics.parquet`**: one row per server per minute for 6 servers over 15 trading days, 09:30–16:00 exchange time (ET, timezone-naive).

| column | type | meaning |
|---|---|---|
| `ts` | datetime | minute bucket start (ET) |
| `server` | str | `srv-1` … `srv-6` |
| `msg_rate_k` | float | inbound market-data messages, thousands/sec, averaged over the minute |
| `p50_us` | float | median internal processing latency in that minute (µs) |
| `p99_us` | float | 99th percentile internal processing latency in that minute (µs) |
| `drops` | int | packets dropped by the NIC in that minute |
| `cpu_pct` | float | CPU utilisation of the busiest core (%) |

**`incidents_labeled.csv`** has incidents from on-call tickets for **days 1–10** (`incident_id, server, start_ts, end_ts, note`). Use it for exploration and tuning.

**`incidents_holdout.csv`** has the same thing for **days 11–15**. Don't open it until your detector is frozen. Use it once, for the final evaluation.

Assume the data was pulled from a metrics warehouse and hasn't been cleaned.

## Tasks

### Part A: Load, clean, explore (≈10 min)
1. Load the metrics and run data-quality checks. Is every (server, minute) present exactly once? Are any values missing? Decide what to do about each problem and justify it.
2. Describe how `msg_rate_k` and `p99_us` behave over the trading day and across servers. Produce a plot of the intraday profile and one plot relating latency to load.
3. Describe the shape of the `p99_us` distribution. Which scale (raw or transformed) would you model it on?

### Part B: Reproduce the current alerting (≈5 min)
1. Implement the baseline detector: for each server, compute a z-score of `p99_us` using mean and std from days 1–10, and flag minutes with `z > 3`.
2. Turn flagged minutes into **alert events**. Consecutive flagged minutes on the same server should be one page, not twenty.
3. Evaluate it against `incidents_labeled.csv` with event-level precision, recall, and detection delay. Write down your matching rule.
4. Where do the false positives come from?

### Part C: Build a better detector (≈15 min)
Design a detector that pages for **server-specific** problems and stays quiet for things that are normal. Explain each design choice. For example:
- What is "expected" latency for a given minute?
- How do you make the centre and scale estimates robust to the anomalies you're hunting?
- How do you reduce single-minute noise without adding much detection delay?
- Should `drops` be treated like latency, or differently?

### Part D: Tune, then evaluate honestly (≈10 min)
1. Pick any thresholds or parameters using days 1–10 only. Show the precision/recall trade-off as you vary the main threshold.
2. Freeze the detector. Evaluate it once on days 11–15 against `incidents_holdout.csv`, and compare it with the Part B baseline on the same days.
3. Look at every false-positive event on the holdout. Are they really false?

### Part E: Slow and persistent changes (≈10 min)
Short incidents aren't the only kind of problem. Investigate whether any server's behaviour changed **persistently** or **gradually** over the 15 days, including things on-call may never have labelled. For anything you find, give:
- which server, when it started, and how big the effect is
- whether it's a one-off step or a trend, and whether it recovers
- a chart that would convince the infra team

### Part F (stretch): How rare is "rare"?
1. Fit a few candidate distributions to your detector's score during normal operation. Compare them with QQ plots and a likelihood-based criterion.
2. Use the best fit to estimate how many false pages per day each threshold implies. Compare that with the naïve Gaussian estimate.

## Follow-up questions the interviewer may ask

1. Why is a global z-score on raw p99 a poor detector here? Name at least two reasons.
2. Why median/MAD instead of mean/std? When would MAD itself break?
3. Your baseline is fitted on days 1–10. What happens to the detector when a server's "normal" legitimately changes? Static vs rolling baseline: what's the trade-off?
4. Point-wise vs event-wise evaluation: which one should the on-call team care about, and why?
5. How would you pick the threshold if you had **no labels at all**?
6. A false page costs 10 minutes of an engineer's time; a missed incident costs $X in degraded trading. How does that change your threshold choice?
7. `p99_us` is already a per-minute percentile. Is the average of 60 per-second p99s the minute's p99? How should the monitoring agent aggregate?
8. How would you run this in real time over 2,000 servers? What state do you keep per server, and what's the per-update cost?
9. Several servers alarm in the same minute. Is that more or less likely to be a real incident? What would you do with it?
10. How would you add unit tests and regression tests to this pipeline?

## 考点（中文）
- **EDA 与数据质量**：重复行要按 (server, ts) 主键去重，不能用全列 `drop_duplicates`；数据缺口要报告，不能填 0；NaN 也要单独处理。
- **季节性与负载**：先想清楚"正常"长什么样。intraday U 型和全市场的 burst 要靠负载模型或季节性 baseline 来解释，不该报警。
- **鲁棒统计**：用 log 变换、median/MAD、persistence filter，并且能说清 heavy-tailed distribution 的影响。
- **评估方法**：event-level precision/recall、detection delay、train/holdout 分开（不偷看）、对 false positive 做人工复核。
- **Change point / drift**：区分 level shift 和 gradual drift（CUSUM、日内斜率），并把结论讲给 infra team 和 trading 听（cross-team communication）。
