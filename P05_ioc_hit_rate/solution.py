"""P05 reference solution -- IOC hit rate vs latency.

Run:  python3 solution.py        (run make_data.py first)
Prints results for Parts A-F and writes figures to ../plots/p05_*.png
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import optimize, stats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p05" / "take_attempts.parquet"
PLOTS = ROOT / "plots"
VENUES = ["EX1", "EX2", "EX3", "EX4", "EX5"]

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 20)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def wilson_ci(k, n, z=1.96):
    """Wilson score interval for a binomial proportion.

    Better than p +/- z*sqrt(p(1-p)/n) when p is near 0/1 or n is small
    (the normal approximation can give intervals outside [0, 1])."""
    k = np.asarray(k, dtype=float)
    n = np.asarray(n, dtype=float)
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return center - half, center + half


def rate_table(df, by):
    """Count-based and quantity-based hit rate with a Wilson 95% CI."""
    g = df.groupby(by, observed=True).agg(
        attempts=("hit", "size"), hits=("hit", "sum"),
        qty_target=("target_qty", "sum"), qty_filled=("filled_qty", "sum"))
    g["hit_rate"] = g.hits / g.attempts
    g["ci_lo"], g["ci_hi"] = wilson_ci(g.hits, g.attempts)
    g["qty_fill_rate"] = g.qty_filled / g.qty_target
    return g[["attempts", "hit_rate", "ci_lo", "ci_hi", "qty_fill_rate"]]


def log_x_ticks(ax):
    ax.set_xscale("log")
    ticks = [0.6, 0.8, 1, 1.5, 2, 3, 4, 6]
    ax.set_xticks(ticks, [str(t) for t in ticks])
    ax.minorticks_off()


def section(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# ----------------------------------------------------------------------------
# load + clean
# ----------------------------------------------------------------------------
def load():
    raw = pd.read_parquet(DATA)
    df = raw.drop_duplicates()                       # identical re-sent records
    dup_ids = df.attempt_id.duplicated().sum()       # same id, different content?
    df = df.copy()
    df["day"] = df["date"].rank(method="dense").astype(int)   # 1..20 trading-day index
    df["hit"] = df["filled_qty"] > 0                 # count-based definition
    # Latency only trustworthy if present and positive. Keep these rows for
    # hit-rate tables (outcome is still valid), drop them from latency work.
    df["latency_ok"] = df["reaction_ns"].notna() & (df["reaction_ns"] > 0)
    df["reaction_us"] = df["reaction_ns"].astype("float64") / 1000.0
    report = {
        "raw_rows": len(raw),
        "exact_duplicates_dropped": len(raw) - len(df),
        "dup_ids_with_different_content": int(dup_ids),
        "reaction_missing": int(df.reaction_ns.isna().sum()),
        "reaction_nonpositive": int((df.reaction_ns <= 0).sum()),
        "outcome_vs_qty_inconsistent": int(
            ((df.outcome == "FILLED") != (df.filled_qty == df.target_qty)).sum()),
    }
    return df, report


# ----------------------------------------------------------------------------
# Part A -- definitions, tables, daily plot
# ----------------------------------------------------------------------------
def part_a(df):
    section("Part A: hit-rate definitions and tables")
    print("count-based hit rate  = attempts with filled_qty > 0 / attempts")
    print("qty-based fill rate   = sum(filled_qty) / sum(target_qty)\n")
    print("Overall:", rate_table(df.assign(all="all"), "all").round(4).to_string(), "\n")
    print(rate_table(df, "venue").round(4).to_string(), "\n")
    print(rate_table(df, "strategy").round(4).to_string())

    daily = df.groupby(["day", "venue"]).agg(n=("hit", "size"), k=("hit", "sum"))
    daily["rate"] = daily.k / daily.n
    pivot = daily.rate.unstack()
    dates = df.groupby("day").date.first().dt.strftime("%m-%d")

    fig, ax = plt.subplots(figsize=(10, 5))
    for v in VENUES:
        d = daily.xs(v, level="venue")
        lo, hi = wilson_ci(d.k, d.n)
        ax.plot(d.index, d.rate, marker="o", label=v)
        ax.fill_between(d.index, lo, hi, alpha=0.15)
    ax.set_xticks(range(1, 21), dates.values, rotation=60)
    ax.set_ylabel("hit rate (count-based)")
    ax.set_title("Daily IOC hit rate by venue (95% Wilson CI)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "p05_daily_hit_rate_by_venue.png", dpi=120)
    plt.close(fig)

    # Flag day-over-day moves far bigger than sampling noise (|z| > 5).
    print("\nDay-over-day jumps larger than 5 standard errors:")
    se = np.sqrt(pivot * (1 - pivot) / daily.n.unstack())
    z = pivot.diff() / np.sqrt(se**2 + se.shift() ** 2)
    for day, venue in zip(*np.where(z.abs().values > 5)):
        d, v = pivot.index[day], pivot.columns[venue]
        print(f"  {v} on day {d} ({dates[d]}): {pivot.iloc[day - 1, venue]:.3f} -> "
              f"{pivot.iloc[day, venue]:.3f}  (z={z.iloc[day, venue]:+.1f})")
    print("  -> day 11 = known S1 release (faster); day 16 EX3 drop is UNEXPLAINED:")
    print("     it hits both strategies, nothing else moved -> likely a new/faster "
          "competitor on EX3. Parts B-E use days 1-15 so this regime change "
          "doesn't contaminate them.")
    return pivot


# ----------------------------------------------------------------------------
# Part B -- hit rate vs our reaction time
# ----------------------------------------------------------------------------
def part_b(df):
    section("Part B: hit rate vs reaction time (days 1-15, valid latency only)")
    d = df[(df.day <= 15) & df.latency_ok].copy()
    # Quantile bins inside each venue: equal counts per bin -> comparable CIs.
    d["bin"] = d.groupby("venue").reaction_us.transform(
        lambda s: pd.qcut(s, 10, labels=False, duplicates="drop"))
    g = d.groupby(["venue", "bin"]).agg(
        t_med=("reaction_us", "median"), n=("hit", "size"), k=("hit", "sum"))
    g["rate"] = g.k / g.n
    g["lo"], g["hi"] = wilson_ci(g.k, g.n)
    table = pd.concat({"median reaction (us)": g.t_med.unstack("venue").round(2),
                       "hit rate": g.rate.unstack("venue").round(3)}, axis=1)
    print(table.to_string())

    fig, ax = plt.subplots(figsize=(9, 5))
    for v in VENUES:
        x = g.xs(v, level="venue")
        ax.errorbar(x.t_med, x.rate, yerr=[x.rate - x.lo, x.hi - x.rate],
                    marker="o", capsize=3, label=v)
    log_x_ticks(ax)
    ax.set_xlabel("our reaction time (us, log scale; bin medians)")
    ax.set_ylabel("hit rate")
    ax.set_title("Hit rate vs reaction time by venue (decile bins, 95% CI)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "p05_hit_vs_reaction.png", dpi=120)
    plt.close(fig)
    print("-> Every venue: hit rate falls as we get slower, and the curve is shifted\n"
          "   right on venues with slower competition (EX1 drops below 50% near 1.4 us,\n"
          "   EX5 stays above 70% until ~3.5 us). Caveat: the slow bins on each venue are\n"
          "   mostly S2 attempts, so bins also differ in strategy/edge mix (see Part D).")
    return g


# ----------------------------------------------------------------------------
# Part C -- Simpson's paradox: S1 vs S2
# ----------------------------------------------------------------------------
def part_c(df):
    section("Part C: S1 vs S2 (days 1-10, before the release)")
    d = df[df.day <= 10]
    overall = d.groupby("strategy").hit.mean()
    by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
    mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
    lat = d[d.latency_ok].groupby("strategy").reaction_us.median()
    print(f"median reaction (us): {lat.round(2).to_dict()}")
    print(f"overall hit rate:     {overall.round(3).to_dict()}")
    print("\nhit rate by venue:\n" + by_venue.round(3).to_string())
    print("\nvenue mix (share of each strategy's attempts):\n" + mix.round(3).to_string())

    # Direct standardization: give S2 the venue mix of S1 (and vice versa).
    s2_on_s1_mix = (by_venue.S2 * mix.S1).sum()
    s1_on_s2_mix = (by_venue.S1 * mix.S2).sum()
    print(f"\nS2 re-weighted to S1's venue mix: {s2_on_s1_mix:.3f}  (vs S1 {overall.S1:.3f})")
    print(f"S1 re-weighted to S2's venue mix: {s1_on_s2_mix:.3f}  (vs S2 {overall.S2:.3f})")
    print("-> Simpson's paradox: S2 looks better overall only because it trades on "
          "EX5 where competition is slow. Within every venue S1 (faster) wins more.")


# ----------------------------------------------------------------------------
# Part D -- models: logistic regression + structural competitor-latency fit
# ----------------------------------------------------------------------------
def fit_logistic(X, y, l2=1e-6, iters=50):
    """Logistic regression by IRLS (Newton's method). Returns beta and std errors."""
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ beta)))
        w = p * (1 - p)
        H = X.T @ (X * w[:, None]) + l2 * np.eye(X.shape[1])
        step = np.linalg.solve(H, X.T @ (y - p) - l2 * beta)
        beta += step
        if np.max(np.abs(step)) < 1e-9:
            break
    se = np.sqrt(np.diag(np.linalg.inv(H)))
    return beta, se


def design_matrix(d):
    cols = {"const": np.ones(len(d)), "reaction_us": d.reaction_us.values,
            "edge_ticks": d.edge_ticks.values.astype(float)}
    for v in VENUES[1:]:                                   # EX1 is the baseline
        cols[f"venue={v}"] = (d.venue == v).values.astype(float)
    return pd.DataFrame(cols)


def fit_competitor_curve(t_us, hit):
    """MLE of  P(hit | t) = (1 - fade) * P(C > t),  C ~ LogNormal(mu, sigma).

    t is OUR reaction time; C is the fastest competitor's latency. Parameters
    are transformed so the optimizer is unconstrained."""
    log_t = np.log(t_us)

    def nll(theta):
        fade = 1 / (1 + np.exp(-theta[0]))
        mu, sigma = theta[1], np.exp(theta[2])
        p = (1 - fade) * stats.norm.sf((log_t - mu) / sigma)
        p = np.clip(p, 1e-9, 1 - 1e-9)
        return -np.sum(hit * np.log(p) + (1 - hit) * np.log(1 - p))

    theta0 = np.array([-2.0, np.log(np.median(t_us)) + 0.3, np.log(0.4)])
    res = optimize.minimize(nll, theta0, method="Nelder-Mead",
                            options={"xatol": 1e-6, "fatol": 1e-6, "maxiter": 5000})
    fade = 1 / (1 + np.exp(-res.x[0]))
    return {"fade": fade, "mu": res.x[1], "sigma": np.exp(res.x[2]), "nll": res.fun}


def structural_p(params, t_us):
    return (1 - params["fade"]) * stats.norm.sf(
        (np.log(t_us) - params["mu"]) / params["sigma"])


def part_d(df, binned):
    section("Part D: models (days 1-15)")
    d = df[(df.day <= 15) & df.latency_ok].copy()

    # (1) logistic regression -------------------------------------------------
    X = design_matrix(d)
    y = d.hit.values.astype(float)
    beta, se = fit_logistic(X.values, y)
    coef = pd.DataFrame({"beta": beta, "se": se, "z": beta / se}, index=X.columns)
    print("Logistic regression  logit P(hit) ~ reaction_us + edge + venue:")
    print(coef.round(4).to_string())
    p_now = 1 / (1 + np.exp(-(X.values @ beta)))
    X_fast = X.copy()
    X_fast["reaction_us"] -= 0.2
    p_fast = 1 / (1 + np.exp(-(X_fast.values @ beta)))
    print(f"avg marginal effect of -200 ns (logistic): {np.mean(p_fast - p_now):+.4f}")
    print(f"odds ratio per 100 ns faster: {np.exp(-0.1 * beta[1]):.3f}")

    # (2) structural fit per venue --------------------------------------------
    print("\nStructural fit per venue: P(hit|t) = (1-fade) * P(C > t), "
          "C ~ LogNormal (C = fastest competitor latency, incl. jitter & edge mix)")
    fits = {}
    for v in VENUES:
        x = d[d.venue == v]
        fits[v] = fit_competitor_curve(x.reaction_us.values, x.hit.values.astype(float))
    fit_df = pd.DataFrame(fits).T
    fit_df["competitor_median_us"] = np.exp(fit_df["mu"].astype(float))
    print(fit_df[["fade", "competitor_median_us", "sigma"]].astype(float).round(3).to_string())

    # plot binned empirical vs fitted curves
    fig, ax = plt.subplots(figsize=(9, 5))
    grid = np.exp(np.linspace(np.log(0.6), np.log(6), 200))
    for i, v in enumerate(VENUES):
        b = binned.xs(v, level="venue")
        ax.plot(b.t_med, b.rate, "o", color=f"C{i}")
        ax.plot(grid, structural_p(fits[v], grid), "-", color=f"C{i}",
                label=f"{v}: median C={np.exp(fits[v]['mu']):.2f}us")
    log_x_ticks(ax)
    ax.set_xlabel("our reaction time (us, log scale)")
    ax.set_ylabel("hit rate")
    ax.set_title("Fitted (1-fade)*P(competitor slower than us) vs binned data")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "p05_competitor_latency_fit.png", dpi=120)
    plt.close(fig)

    # (3) value of shaving 200 ns -----------------------------------------------
    # Use days 11-15: the most recent latency profile that predates the EX3 change.
    recent = d[d.day >= 11].copy()
    n_days = recent.day.nunique()
    p_now = np.empty(len(recent))
    p_fast = np.empty(len(recent))
    for v in VENUES:
        m = (recent.venue == v).values
        t = recent.reaction_us.values[m]
        p_now[m] = structural_p(fits[v], t)
        p_fast[m] = structural_p(fits[v], np.maximum(t - 0.2, 0.05))
    recent["dp"] = p_fast - p_now
    # $ per tick per contract: $0.01 tick * 100 multiplier = $1.00
    tick_value_per_contract = 0.01 * 100
    # expected contracts if hit ~ average fill fraction * target size
    fill_frac = (df.filled_qty[df.hit] / df.target_qty[df.hit]).mean()
    recent["dollars"] = (recent.dp * recent.target_qty * fill_frac
                         * recent.edge_ticks * tick_value_per_contract)
    per_venue = recent.groupby("venue").agg(
        extra_fills_per_day=("dp", lambda s: s.sum() / n_days),
        extra_dollars_per_day=("dollars", lambda s: s.sum() / n_days))
    print("\nValue of being 200 ns faster (both strategies, days 11-15 attempt mix):")
    print(per_venue.round(1).to_string())
    print(f"TOTAL: +{recent.dp.sum() / n_days:,.0f} fills/day, "
          f"~${recent.dollars.sum() / n_days:,.0f}/day theoretical edge "
          f"(assumes edge fully captured, no adverse selection, competitors don't react)")
    return fits


# ----------------------------------------------------------------------------
# Part E -- impact of the day-11 release
# ----------------------------------------------------------------------------
def part_e(df):
    section("Part E: impact of the day-11 release on S1 hit rate")
    d = df[df.day <= 15].copy()          # exclude the EX3 regime change (day 16+)
    d["period"] = np.where(d.day >= 11, "after", "before")

    rates = d.groupby(["strategy", "period", "venue"]).hit.agg(["mean", "size"])
    def stratified(strategy, period, weights):
        r = rates.loc[(strategy, period)]["mean"]
        return float((r * weights).sum())

    w_s1 = d[(d.strategy == "S1") & (d.period == "before")].venue.value_counts(normalize=True)
    w_s2 = d[(d.strategy == "S2") & (d.period == "before")].venue.value_counts(normalize=True)
    naive_s1 = (d[(d.strategy == "S1") & (d.period == "after")].hit.mean()
                - d[(d.strategy == "S1") & (d.period == "before")].hit.mean())
    strat_s1 = stratified("S1", "after", w_s1) - stratified("S1", "before", w_s1)
    strat_s2 = stratified("S2", "after", w_s2) - stratified("S2", "before", w_s2)
    did = strat_s1 - strat_s2

    lat = d[d.latency_ok & (d.strategy == "S1")].groupby("period").reaction_us.median()
    print(f"S1 median reaction: before {lat['before']:.3f} us, after {lat['after']:.3f} us "
          f"(-{(lat['before'] - lat['after']) * 1000:.0f} ns)")
    mix = pd.crosstab(d[d.strategy == "S1"].period, d[d.strategy == "S1"].venue,
                      normalize="index")
    print("S1 venue mix by period:\n" + mix.round(3).to_string())
    print(f"\n(1) naive S1 after-before:                 {naive_s1:+.4f}")
    print(f"(2) venue-stratified S1 after-before:      {strat_s1:+.4f}")
    print(f"(3) control: S2 stratified after-before:   {strat_s2:+.4f}  (market got harder)")
    print(f"(4) diff-in-diff (2)-(3):                  {did:+.4f}")

    # Day-block bootstrap: days are the independent units (intraday outcomes correlate).
    rng = np.random.default_rng(0)
    cell = d.groupby(["day", "strategy", "venue"]).hit.agg(["sum", "size"])
    before_days = np.arange(1, 11)
    after_days = np.arange(11, 16)
    boots = []
    for _ in range(400):
        days = np.concatenate([rng.choice(before_days, 10), rng.choice(after_days, 5)])
        c = pd.concat([cell.loc[day] for day in days])
        c = c.assign(period=np.repeat(np.where(days >= 11, "after", "before"),
                                      [len(cell.loc[day]) for day in days]))
        agg = c.groupby(["strategy", "period", "venue"])[["sum", "size"]].sum()
        r = agg["sum"] / agg["size"]
        s1 = (r.loc[("S1", "after")] * w_s1).sum() - (r.loc[("S1", "before")] * w_s1).sum()
        s2 = (r.loc[("S2", "after")] * w_s2).sum() - (r.loc[("S2", "before")] * w_s2).sum()
        boots.append(s1 - s2)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"    day-block bootstrap 95% CI for DiD:    [{lo:+.4f}, {hi:+.4f}]")

    # What goes wrong if we ignore the day-16 change:
    d20 = df.assign(period=np.where(df.day >= 11, "after", "before"))
    bad = (d20[(d20.strategy == "S1") & (d20.period == "after")].hit.mean()
           - d20[(d20.strategy == "S1") & (d20.period == "before")].hit.mean())
    print(f"\nnaive S1 after-before using days 1-20 (WRONG window): {bad:+.4f} "
          "-- the EX3 competitor shock leaks into the 'after' period")
    return did


# ----------------------------------------------------------------------------
# Part F -- trader summary
# ----------------------------------------------------------------------------
def part_f(df, fits, did):
    section("Part F: summary for traders")
    daily = df.groupby(["day", "venue"]).hit.mean().unstack()
    dates = df.groupby("day").date.first().dt.strftime("%m-%d")
    fig, ax = plt.subplots(figsize=(10, 5))
    for v in VENUES:
        ax.plot(daily.index, daily[v], marker="o", lw=2 if v == "EX3" else 1,
                label=v, alpha=1 if v == "EX3" else 0.6)
    ax.axvline(10.5, color="gray", ls="--")
    ax.text(10.6, 0.25, "S1 release\n(-300 ns)", fontsize=9)
    ax.axvline(15.5, color="red", ls="--")
    ax.text(15.6, 0.25, "EX3: new faster\ncompetitor?", color="red", fontsize=9)
    ax.set_xticks(range(1, 21), dates.values, rotation=60)
    ax.set_ylim(0.2, 1.0)
    ax.set_ylabel("daily hit rate")
    ax.set_title("IOC hit rate by venue: release helped; EX3 collapsed from Nov 9")
    ax.legend(ncol=5, loc="lower left")
    fig.tight_layout()
    fig.savefig(PLOTS / "p05_summary_for_traders.png", dpi=120)
    plt.close(fig)

    ex3_before = daily.loc[11:15, "EX3"].mean()
    ex3_after = daily.loc[16:20, "EX3"].mean()
    print(
        "Our IOC hit rate is driven by how our reaction time compares with the fastest\n"
        "competitor on each venue: EX1 is the most contested (competitor median ~"
        f"{np.exp(fits['EX1']['mu']):.1f} us), EX5 the least (~{np.exp(fits['EX5']['mu']):.1f} us).\n"
        "S2's higher headline hit rate is a venue-mix artifact; venue by venue S1 wins more.\n"
        f"The day-11 release raised S1's like-for-like hit rate by ~{did * 100:.0f} pp "
        "(diff-in-diff vs S2).\n"
        f"Since Nov 9 our EX3 hit rate fell from {ex3_before:.0%} to {ex3_after:.0%} on "
        "both strategies with no change on our side:\nmost likely a new competitor faster "
        "than ~1.1 us. Recommend: review EX3 sizing/edge thresholds, and\nprioritise "
        "latency work for EX1/EX3 where each 100 ns is worth the most.")


def main():
    PLOTS.mkdir(parents=True, exist_ok=True)
    df, report = load()
    section("Data cleaning")
    for k, v in report.items():
        print(f"  {k:32s} {v}")
    part_a(df)
    binned = part_b(df)
    part_c(df)
    fits = part_d(df, binned)
    did = part_e(df)
    part_f(df, fits, did)


if __name__ == "__main__":
    main()
