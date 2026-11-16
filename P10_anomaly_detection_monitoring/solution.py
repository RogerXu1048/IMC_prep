"""P10 reference solution — anomaly detection on trading-server monitoring metrics.

Run:  python3 solution.py        (after python3 make_data.py)

Structure mirrors the handout: load/clean -> A EDA -> B naive detector -> C load-adjusted
detector -> D tune on labeled days, evaluate on holdout -> E chronic changes -> F tail fitting.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p10"
PLOTS = ROOT / "plots"
PLOTS.mkdir(exist_ok=True)

TRAIN_LAST_DAY = pd.Timestamp("2026-11-06")   # days 1-10 labeled, days 11-15 holdout
MAD_TO_SIGMA = 1.4826                          # MAD * 1.4826 = sigma for a normal distribution


# ----------------------------------------------------------------------------- load / clean
def load():
    df = pd.read_parquet(DATA / "metrics.parquet")
    n_raw = len(df)
    # A (server, ts) key must be unique. Re-sent rows have slightly different values, so
    # drop_duplicates() on all columns would NOT catch them -> dedupe on the key.
    dup = df.duplicated(["server", "ts"], keep="first")
    df = df[~dup].sort_values(["server", "ts"]).reset_index(drop=True)
    df["date"] = df["ts"].dt.normalize()
    df["minute"] = (df["ts"] - df["date"] - pd.Timedelta(hours=9, minutes=30)) // pd.Timedelta(minutes=1)

    per_day = df.groupby(["server", "date"]).size()
    incomplete = per_day[per_day < 390]
    print("== Load / data quality")
    print(f"raw rows={n_raw}, duplicate (server,ts) rows dropped={int(dup.sum())}, kept={len(df)}")
    print(f"missing p99 values={int(df.p99_us.isna().sum())}  (left as NaN: never impute zeros for latency)")
    for (srv, day), n in incomplete.items():
        print(f"incomplete server-day: {srv} {day.date()} has {n}/390 minutes -> monitoring gap, report it")

    inc_train = pd.read_csv(DATA / "incidents_labeled.csv", parse_dates=["start_ts", "end_ts"])
    inc_test = pd.read_csv(DATA / "incidents_holdout.csv", parse_dates=["start_ts", "end_ts"])
    return df, inc_train, inc_test


def is_train(df):
    return df["date"] <= TRAIN_LAST_DAY


def incident_mask(df, incidents):
    """Boolean Series: row falls inside a labeled incident window on the same server."""
    mask = pd.Series(False, index=df.index)
    for row in incidents.itertuples():  # few incidents -> a loop is fine and readable
        mask |= (df.server == row.server) & (df.ts >= row.start_ts) & (df.ts <= row.end_ts)
    return mask


# ----------------------------------------------------------------------------- A. EDA
def part_a_eda(df):
    print("\n== Part A: EDA")
    prof = df.groupby("minute")[["msg_rate_k", "p99_us"]].quantile([0.25, 0.5, 0.75]).unstack()
    open_p99 = df[df.minute < 15].p99_us.median()
    mid_p99 = df[(df.minute >= 120) & (df.minute < 240)].p99_us.median()
    print(f"median p99: first 15 min = {open_p99:.1f}us, midday = {mid_p99:.1f}us "
          f"-> strong intraday seasonality (U shape, driven by message rate)")
    rho = stats.spearmanr(df.msg_rate_k, df.p99_us, nan_policy="omit").statistic
    print(f"Spearman corr(msg_rate, p99) = {rho:.2f} -> load explains most of the latency variation")
    print(f"p99 skew = {df.p99_us.skew():.1f}; log(p99) skew = {np.log(df.p99_us).skew():.1f} -> work in log space")

    fig, ax = plt.subplots(1, 3, figsize=(16, 4.2))
    for i, col in enumerate(["msg_rate_k", "p99_us"]):
        x = prof.index
        ax[i].plot(x, prof[(col, 0.5)], color="C0")
        ax[i].fill_between(x, prof[(col, 0.25)], prof[(col, 0.75)], alpha=0.3, color="C0")
        ax[i].set(title=f"{col} by minute of day (median, IQR)", xlabel="minutes since 09:30 ET")
    sample = df.sample(6000, random_state=0)
    ax[2].scatter(sample.msg_rate_k, sample.p99_us, s=3, alpha=0.4)
    ax[2].set(xscale="log", yscale="log", xlabel="msg rate (k/s)", ylabel="p99 (us)", title="p99 vs load (log-log)")
    fig.tight_layout()
    fig.savefig(PLOTS / "p10_eda_intraday.png", dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- event helpers
def flags_to_events(df, flag_col, score_col, max_gap_min=2):
    """Collapse flagged minutes into events: same server, gaps <= max_gap_min minutes.
    One on-call page per event, so precision is measured on events, not minutes."""
    f = df.loc[df[flag_col], ["server", "ts", score_col]].sort_values(["server", "ts"])
    if f.empty:
        return pd.DataFrame(columns=["server", "start", "end", "n_min", "peak"])
    new_event = (f.server != f.server.shift()) | (f.ts.diff() > pd.Timedelta(minutes=max_gap_min))
    f["event_id"] = new_event.cumsum()
    return f.groupby("event_id").agg(server=("server", "first"), start=("ts", "min"),
                                     end=("ts", "max"), n_min=("ts", "size"),
                                     peak=(score_col, "max")).reset_index(drop=True)


def evaluate(events, incidents, slack=pd.Timedelta(minutes=2)):
    """Event-level precision/recall + detection delay. O(E*I), both are small."""
    detected, delays = 0, []
    ev_hit = np.zeros(len(events), dtype=bool)
    for inc in incidents.itertuples():
        m = ((events.server == inc.server) & (events.start <= inc.end_ts + slack)
             & (events.end >= inc.start_ts - slack)).to_numpy()
        if m.any():
            detected += 1
            delays.append(max(pd.Timedelta(0), events.start[m].min() - inc.start_ts) / pd.Timedelta(minutes=1))
        ev_hit |= m
    precision = ev_hit.mean() if len(events) else float("nan")
    recall = detected / len(incidents)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return dict(events=len(events), false_pos=int((~ev_hit).sum()), detected=detected,
                incidents=len(incidents), precision=precision, recall=recall, f1=f1,
                median_delay_min=float(np.median(delays)) if delays else float("nan")), ev_hit


def fmt(r):
    return (f"events={r['events']:3d} FP={r['false_pos']:3d} detected={r['detected']}/{r['incidents']} "
            f"P={r['precision']:.2f} R={r['recall']:.2f} F1={r['f1']:.2f} delay={r['median_delay_min']:.0f}min")


# ----------------------------------------------------------------------------- B. naive detector
def part_b_naive(df, inc_train):
    print("\n== Part B: naive detector (per-server z-score of raw p99, threshold 3)")
    tr = df[is_train(df)]
    mu = tr.groupby("server").p99_us.mean()
    sd = tr.groupby("server").p99_us.std()
    df["z_naive"] = (df.p99_us - df.server.map(mu)) / df.server.map(sd)
    df["flag_naive"] = df.z_naive > 3
    ev = flags_to_events(df[is_train(df)], "flag_naive", "z_naive")
    res, hit = evaluate(ev, inc_train)
    print(fmt(res))
    fp = ev[~hit]
    fp_open = (fp.start.dt.hour * 60 + fp.start.dt.minute < 9 * 60 + 30 + 20).mean()
    print(f"share of false-positive events starting in the first 20 min of the day: {fp_open:.0%}")
    news = df[(df.date == "2026-11-02") & (df.minute.between(30, 37)) & df.flag_naive].server.nunique()
    print(f"2026-11-02 10:00-10:07: naive detector fires on {news}/6 servers at once "
          "-> market-wide load burst (e.g. 10:00 economic release), not a server incident")
    return res


# ----------------------------------------------------------------------------- C. load-adjusted detector
def fit_baseline(df, known_incidents):
    """Per-server model of log(p99) as a function of log(msg_rate) on clean training minutes.

    Latency rises with load (queueing), so the *expected* p99 at 09:31 is much higher than at
    noon. Modeling load removes the intraday seasonality AND market-wide bursts in one step.
    Degree-2 polynomial because the relation is convex at high load (queueing knee)."""
    clean = is_train(df) & ~known_incidents & df.p99_us.notna()
    df["resid"] = np.nan
    for srv, g in df[clean].groupby("server"):
        coef = np.polyfit(np.log(g.msg_rate_k), np.log(g.p99_us), 2)
        rows = df.server == srv
        df.loc[rows, "resid"] = np.log(df.loc[rows, "p99_us"]) - np.polyval(coef, np.log(df.loc[rows, "msg_rate_k"]))
    # robust centre/scale: median & MAD are not dragged around by the very outliers we hunt
    base = df[clean]
    med = base.groupby("server").resid.median()
    mad = base.groupby("server").resid.agg(lambda r: np.median(np.abs(r - r.median())))
    df["z"] = (df.resid - df.server.map(med)) / (MAD_TO_SIGMA * df.server.map(mad))

    # packet drops: count data -> Poisson with load-dependent mean, estimated per load decile
    df["load_bin"] = pd.qcut(df.msg_rate_k, 10, labels=False)
    lam = base.assign(load_bin=df.loc[base.index, "load_bin"]).groupby("load_bin").drops.mean()
    df["drop_thr"] = stats.poisson.ppf(1 - 1e-5, df.load_bin.map(lam).clip(lower=0.01))
    return df


def apply_rules(df, k):
    g = df.groupby("server").z
    hi = df.z > k
    persistent = hi & ((g.shift(1) > k) | (g.shift(-1) > k))   # >= 2 consecutive minutes
    df["flag_lat"] = persistent | (df.z > 2 * k)                # or one extreme minute
    df["flag_drop"] = df.drops > np.maximum(df.drop_thr, 5)
    df["flag"] = df.flag_lat | df.flag_drop
    df["score"] = df.z.fillna(0) + df.flag_drop * 100            # for event "peak" only
    return df


def part_c_d_adjusted(df, inc_train, inc_test):
    print("\n== Part C: load-adjusted robust detector")
    df = fit_baseline(df, incident_mask(df, inc_train))
    by_minute = df[is_train(df)].groupby(df.minute // 30).resid.median()
    print("median residual by half-hour of day (should be ~flat if load explains seasonality):")
    print("  " + " ".join(f"{v:+.02f}" for v in by_minute.values))

    print("\n== Part D: tune threshold on labeled days 1-10, evaluate ONCE on holdout days 11-15")
    best = None
    for k in [2.5, 3, 3.5, 4, 5, 6, 8, 10, 12]:
        df = apply_rules(df, k)
        res, _ = evaluate(flags_to_events(df[is_train(df)], "flag", "score"), inc_train)
        print(f"  k={k:<4} {fmt(res)}")
        if best is None or res["f1"] > best[1]["f1"] + 1e-9:
            best = (k, res)
    k = best[0]
    df = apply_rules(df, k)
    test_ev = flags_to_events(df[~is_train(df)], "flag", "score")
    res, hit = evaluate(test_ev, inc_test)
    print(f"chosen k={k}. HOLDOUT: {fmt(res)}")
    print("holdout false-positive events (investigate these, labels can be incomplete):")
    print(test_ev[~hit].to_string(index=False))

    naive_ev = flags_to_events(df[~is_train(df)], "flag_naive", "z_naive")
    print(f"naive detector on holdout for comparison: {fmt(evaluate(naive_ev, inc_test)[0])}")
    plot_example_day(df, k)
    return df, k


def plot_example_day(df, k):
    day = pd.Timestamp("2026-11-02")
    d = df[(df.server == "srv-1") & (df.date == day)]
    fig, ax = plt.subplots(3, 1, figsize=(12, 7), sharex=True)
    ax[0].plot(d.ts, d.msg_rate_k); ax[0].set_ylabel("msg rate (k/s)")
    ax[1].plot(d.ts, d.p99_us, color="C1"); ax[1].set_ylabel("p99 (us)")
    ax[1].scatter(d.ts[d.flag_naive], d.p99_us[d.flag_naive], color="red", s=14, label="naive z>3 flags")
    ax[1].legend()
    ax[2].plot(d.ts, d.z, color="C2"); ax[2].axhline(k, ls="--", color="grey", label=f"chosen k={k}")
    ax[2].legend()
    ax[2].set_ylabel("load-adjusted robust z")
    ax[0].set_title("srv-1 on 2026-11-02: naive flags fire on the market-wide 10:00 burst; load-adjusted z stays calm")
    fig.tight_layout()
    fig.savefig(PLOTS / "p10_naive_vs_adjusted.png", dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- E. chronic changes
def part_e_chronic(df):
    print("\n== Part E: slow / persistent behaviour changes")
    daily = df.pivot_table(index="server", columns="date", values="resid", aggfunc="median")
    daily.columns = [c.strftime("%m-%d") for c in daily.columns]
    print("daily median residual (log p99 vs load model):")
    print(daily.round(2).to_string())

    # CUSUM on the daily median residual. Baseline = first 5 days per server.
    print("\nupper CUSUM alarms (k=0.5 sigma, h=5 sigma):")
    for srv, row in daily.iterrows():
        x = row.to_numpy()
        mu0, sd0 = x[:5].mean(), max(x[:5].std(ddof=1), 0.01)
        s, alarm = 0.0, None
        for i, v in enumerate(x):
            s = max(0.0, s + (v - mu0) / sd0 - 0.5)
            if s > 5 and alarm is None:
                alarm = daily.columns[i]
        if alarm:
            print(f"  {srv}: CUSUM alarm first on {alarm}")

    # Level shift vs drift: within-day slope of the residual (per server-day)
    slopes = (df.dropna(subset=["resid"]).groupby(["server", "date"])
                .apply(lambda g: np.polyfit(g.minute, g.resid, 1)[0] * 390, include_groups=False))
    big = slopes[slopes > 0.3]
    print("server-days whose residual rises > 0.3 log-units from open to close (gradual degradation):")
    for (srv, day), v in big.items():
        print(f"  {srv} {day.date()}: +{v:.2f} (x{np.exp(v):.1f} p99 by the close) -> leak-like drift, resets overnight")
    shift = daily.loc["srv-4"]
    print(f"srv-4 median residual days 1-8 = {shift.iloc[:8].mean():+.2f}, days 9-15 = {shift.iloc[8:].mean():+.2f} "
          f"-> step change of x{np.exp(shift.iloc[8:].mean() - shift.iloc[:8].mean()):.2f} on 11-05 (deploy?)")

    fig, ax = plt.subplots(figsize=(12, 3.6))
    im = ax.imshow(daily.to_numpy(), cmap="RdBu_r", vmin=-0.6, vmax=0.6, aspect="auto")
    ax.set_xticks(range(daily.shape[1]), daily.columns, rotation=45)
    ax.set_yticks(range(daily.shape[0]), daily.index)
    ax.set_title("daily median load-adjusted residual of log p99 (red = slower than expected)")
    fig.colorbar(im)
    fig.tight_layout()
    fig.savefig(PLOTS / "p10_daily_residual_heatmap.png", dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- F. distribution fitting
def part_f_tails(df, inc_train):
    print("\n== Part F: what does the 'normal' residual distribution look like?")
    clean = is_train(df) & ~incident_mask(df, inc_train) & df.z.notna() & ~((df.server == "srv-4") & (df.date >= "2026-11-05"))
    z = df.loc[clean, "z"].to_numpy()
    fits = {
        "normal": (stats.norm, stats.norm.fit(z)),
        "student_t": (stats.t, stats.t.fit(z)),
        "laplace": (stats.laplace, stats.laplace.fit(z)),
    }
    for name, (dist, params) in fits.items():
        ll = dist.logpdf(z, *params).sum()
        aic = 2 * len(params) - 2 * ll
        print(f"  {name:9s} params={tuple(round(float(p), 3) for p in params)}  AIC={aic:,.0f}")
    t_df = fits["student_t"][1][0]
    print(f"  -> Student-t wins (df~{t_df:.1f}): heavy tails, so a Gaussian 3-sigma rule underestimates false alarms")
    n_per_day = 390 * 6
    for k in [3, 4, 5]:
        emp = (z > k).mean()
        p_norm = stats.norm.sf(k, *fits["normal"][1])
        p_t = stats.t.sf(k, *fits["student_t"][1])
        print(f"  P(z>{k}): empirical={emp:.2e}  normal={p_norm:.2e}  t={p_t:.2e}  "
              f"-> ~{emp * n_per_day:.1f} single-minute exceedances/day across 6 servers")

    fig, ax = plt.subplots(1, 2, figsize=(11, 4.5))
    stats.probplot(z, dist="norm", plot=ax[0]); ax[0].set_title("QQ vs normal (tails bend away)")
    stats.probplot(z, dist=stats.t, sparams=(t_df,), plot=ax[1]); ax[1].set_title(f"QQ vs Student-t(df={t_df:.1f})")
    fig.tight_layout()
    fig.savefig(PLOTS / "p10_residual_qq.png", dpi=110)
    plt.close(fig)


def main():
    df, inc_train, inc_test = load()
    part_a_eda(df)
    part_b_naive(df, inc_train)
    df, _ = part_c_d_adjusted(df, inc_train, inc_test)
    part_e_chronic(df)
    part_f_tails(df, inc_train)
    print(f"\nplots written to {PLOTS}")


if __name__ == "__main__":
    main()
