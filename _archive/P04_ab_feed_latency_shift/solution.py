"""P04 reference solution: A/B feed arbitration and latency distribution shift.

Run:  python3 solution.py        (after python3 make_data.py)
Figures are written to ../plots/p04_*.png
"""
from pathlib import Path
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p04"
PLOTS = ROOT / "plots"
PLOTS.mkdir(exist_ok=True)

PCTS = [50, 90, 99, 99.9]
RATE_WINDOW_NS = 100_000           # "instantaneous" rate = msgs in the previous 100 us
RATE_BUCKETS = [0, 10, 25, 50, 75, 100, 150, 1_000]


def pct_table(series_by_group):
    """Percentile table in microseconds for a groupby object."""
    out = series_by_group.quantile([p / 100 for p in PCTS]).unstack()
    out.columns = [f"p{p:g}" for p in PCTS]
    return out.round(2)


# ----------------------------------------------------------------------------- Part A
def part_a_load_and_check():
    """Load both captures, report integrity problems, return de-duplicated frames."""
    print("\n=== Part A: load + sanity checks ===")
    lines = {}
    for line in ("A", "B"):
        df = pd.read_parquet(DATA / f"feed_{line}.parquet")
        df["date"] = df["date"].astype(str)
        df["line"] = line
        lines[line] = df

    rows = []
    for line, df in lines.items():
        for date, g in df.groupby("date"):
            seq = g["seq"].to_numpy()                       # file is in rx (arrival) order
            dups = int(g.duplicated("seq").sum())
            # out-of-order: a seq smaller than the max seen so far (ignoring exact dups)
            uniq_first = g.drop_duplicates("seq")["seq"].to_numpy()
            out_of_order = int((uniq_first < np.maximum.accumulate(uniq_first)).sum())
            rx_sorted = bool((np.diff(g["rx_ns"].to_numpy()) >= 0).all())
            rows.append(dict(line=line, date=date, rows=len(g), unique_seq=len(uniq_first),
                             duplicates=dups, out_of_order=out_of_order,
                             rx_monotonic=rx_sorted, max_seq=int(seq.max())))
    print(pd.DataFrame(rows).to_string(index=False))

    # De-duplicate: keep the FIRST arrival of each seq per line (a dup is never useful).
    # Also add day-relative integer timestamps. Epoch ns (~1.8e18) is NOT exactly
    # representable in float64 (ulp = 256 ns), and an outer merge with missing values
    # silently converts int64 -> float64. Day-relative ns (< 2^53) survive that exactly.
    for line in lines:
        df = (lines[line].sort_values(["date", "rx_ns"])
              .drop_duplicates(["date", "seq"], keep="first").copy())
        origin = pd.to_datetime(df["date"]).astype("datetime64[ns]").astype("int64")
        df["exch_rel"] = df["exch_ns"] - origin
        df["rx_rel"] = df["rx_ns"] - origin
        lines[line] = df

    # Losses: the universe of seqs is 1..max over the union of both lines.
    # (A seq lost at the very end of the day on both lines is invisible -- edge case.)
    loss_rows, both_lost = [], {}
    for date in sorted(lines["A"]["date"].unique()):
        a = set(lines["A"].loc[lines["A"]["date"] == date, "seq"])
        b = set(lines["B"].loc[lines["B"]["date"] == date, "seq"])
        universe = set(range(1, max(max(a), max(b)) + 1))
        miss_a, miss_b = universe - a, universe - b
        both = sorted(miss_a & miss_b)
        both_lost[date] = both
        loss_rows.append(dict(date=date, total=len(universe),
                              lost_A=len(miss_a), lost_A_pct=100 * len(miss_a) / len(universe),
                              lost_B=len(miss_b), lost_B_pct=100 * len(miss_b) / len(universe),
                              lost_both=len(both), runs_both=count_runs(both),
                              B_runs=count_runs(sorted(miss_b)),
                              B_mean_run=len(miss_b) / max(count_runs(sorted(miss_b)), 1)))
    print("\nLoss summary (pct of all sequence numbers):")
    print(pd.DataFrame(loss_rows).round(3).to_string(index=False))
    print("Seqs lost on BOTH lines (need retransmission / snapshot recovery):",
          {d: s[:8] + (["..."] if len(s) > 8 else []) for d, s in both_lost.items()})
    return lines["A"], lines["B"]


def count_runs(sorted_seqs):
    """Number of maximal runs of consecutive integers (clustered vs iid loss)."""
    if not sorted_seqs:
        return 0
    arr = np.asarray(sorted_seqs)
    return int(1 + (np.diff(arr) != 1).sum())


# ----------------------------------------------------------------------------- Part B
def part_b_latency(A, B):
    print("\n=== Part B: per-line latency (rx_ns - exch_ns) ===")
    feeds = pd.concat([A, B], ignore_index=True)
    feeds["lat_us"] = (feeds["rx_ns"] - feeds["exch_ns"]) / 1e3
    print(pct_table(feeds.groupby(["line", "date"])["lat_us"]).to_string())
    print("mean by line/date:", feeds.groupby(["line", "date"])["lat_us"].mean().round(2).to_dict())

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    bins = np.logspace(np.log10(15), np.log10(feeds["lat_us"].max()), 120)
    for (line, date), g in feeds.groupby(["line", "date"]):
        label = f"{line} {date}"
        axes[0].hist(g["lat_us"], bins=bins, histtype="step", density=True, label=label)
        x = np.sort(g["lat_us"].to_numpy())
        y = np.arange(1, len(x) + 1) / len(x)
        axes[1].plot(x, 1 - y, label=label)                       # CCDF shows the tail
    axes[0].set(xscale="log", yscale="log", xlabel="latency (us, log)", ylabel="density",
                title="Latency histogram (log-log)")
    axes[1].set(xscale="log", yscale="log", xlabel="latency (us, log)",
                ylabel="P(latency > x)", title="Complementary ECDF (tail view)")
    for ax in axes:
        ax.legend()
        ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(PLOTS / "p04_latency_distributions.png", dpi=110)
    plt.close(fig)

    # Clock-drift check: the per-minute MINIMUM latency strips queueing and noise,
    # leaving (true min path latency + clock offset). If it trends identically on both
    # lines, the trend is the clock, not the network.
    feeds["minute"] = (feeds["exch_ns"] - feeds.groupby("date")["exch_ns"].transform("min")) // 60e9
    mins = feeds.groupby(["date", "line", "minute"])["lat_us"].min().reset_index()
    print("\nRolling-minimum trend (slope of per-minute min latency, us per hour):")
    fig, ax = plt.subplots(figsize=(10, 4))
    for (date, line), g in mins.groupby(["date", "line"]):
        slope, intercept = np.polyfit(g["minute"] / 60, g["lat_us"], 1)
        print(f"  {line} {date}: slope={slope:+.2f} us/h, start~{intercept:.2f} us, "
              f"change over capture={slope * g['minute'].max() / 60:+.2f} us")
        ax.plot(g["minute"], g["lat_us"], ".", ms=3, label=f"{line} {date}")
    ax.set(xlabel="minutes since 14:30 UTC", ylabel="min latency in minute (us)",
           title="Per-minute minimum latency: same upward drift on both lines = clock drift")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(PLOTS / "p04_min_latency_drift.png", dpi=110)
    plt.close(fig)
    return feeds


# ----------------------------------------------------------------------------- Part C
def part_c_arbitration(A, B):
    print("\n=== Part C: A/B arbitration ===")
    cols = ["date", "seq", "exch_rel", "rx_rel"]
    m = pd.merge(A[cols].rename(columns={"exch_rel": "exch_ns", "rx_rel": "rx_ns"}),
                 B[cols].rename(columns={"exch_rel": "exch_ns", "rx_rel": "rx_ns"}),
                 on=["date", "seq"], how="outer", suffixes=("_A", "_B"))
    # NB: from here on *_ns columns are day-relative (float64 after the outer merge, exact)
    m["exch_ns"] = m["exch_ns_A"].fillna(m["exch_ns_B"])
    m["rx_first"] = m[["rx_ns_A", "rx_ns_B"]].min(axis=1)       # NaN-aware min
    m["winner"] = np.select(
        [m["rx_ns_B"].isna(), m["rx_ns_A"].isna(), m["rx_ns_A"] < m["rx_ns_B"],
         m["rx_ns_A"] > m["rx_ns_B"]],
        ["A only", "B only", "A", "B"], default="tie")
    print("Winner share by day (pct):")
    print((pd.crosstab(m["date"], m["winner"], normalize="index") * 100).round(3).to_string())

    m["lat_A"] = (m["rx_ns_A"] - m["exch_ns"]) / 1e3
    m["lat_B"] = (m["rx_ns_B"] - m["exch_ns"]) / 1e3
    m["lat_arb"] = (m["rx_first"] - m["exch_ns"]) / 1e3
    long = m.melt(id_vars=["date"], value_vars=["lat_A", "lat_B", "lat_arb"],
                  var_name="feed", value_name="lat_us").dropna()
    print("\nLatency percentiles: each line alone vs arbitrated (us):")
    print(pct_table(long.groupby(["date", "feed"])["lat_us"]).to_string())
    print("Careful: on day 2 the arbitrated p99 is ABOVE line B's p99. B tail-drops exactly the\n"
          "packets stuck in deep queues; arbitration recovers them from A, so they re-enter the\n"
          "sample. B-alone percentiles are survivorship-biased (only delivered packets count).")
    lost_b = m["rx_ns_B"].isna() & m["rx_ns_A"].notna()
    lat_lost = m.loc[lost_b, "lat_A"]
    print(f"Line-A latency of packets that B lost: median {lat_lost.median():.1f} us "
          f"(vs {m['lat_A'].median():.1f} us overall) -> B's losses happen inside microbursts")

    both = m.dropna(subset=["rx_ns_A", "rx_ns_B"])
    gap = (both["rx_ns_A"] - both["rx_ns_B"]) / 1e3           # >0 means B arrived first
    print("\nA - B arrival difference when both present (us), by day:")
    print(gap.groupby(both["date"]).quantile([0.01, 0.1, 0.5, 0.9, 0.99]).unstack().round(2)
          .to_string())

    fig, ax = plt.subplots(figsize=(9, 4))
    for date, g in gap.groupby(both["date"]):
        ax.hist(g.clip(-15, 15), bins=150, histtype="step", density=True, label=date)
    ax.axvline(0, color="k", lw=0.8)
    ax.set(xlabel="rx_A - rx_B (us, clipped to +-15); >0 means B first", ylabel="density",
           title="Who wins the A/B race?")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "p04_ab_race.png", dpi=110)
    plt.close(fig)
    return m


# ----------------------------------------------------------------------------- Part D
def add_rate(merged):
    """msgs sent by the exchange in the previous 100us (per day), from exch_ns of the
    union feed -- we must use the union, otherwise losses bias the rate on one line."""
    merged = merged.sort_values(["date", "seq"]).copy()
    rate = np.empty(len(merged), dtype=np.int64)
    pos = 0
    for _, g in merged.groupby("date", sort=True):
        e = g["exch_ns"].to_numpy()
        # count of messages with exch_ns in (t - 100us, t]; O(n log n) via binary search
        r = np.arange(len(e)) - np.searchsorted(e, e - RATE_WINDOW_NS, side="right") + 1
        rate[pos:pos + len(e)] = r
        pos += len(e)
    merged["rate_100us"] = rate
    merged["rate_bucket"] = pd.cut(merged["rate_100us"], RATE_BUCKETS, right=False)
    return merged


def block_bootstrap_diff(x1, blocks1, x2, blocks2, q, n_boot, rng):
    """CI for quantile(x2) - quantile(x1), resampling whole 1-second blocks.

    Latencies inside a microburst are strongly autocorrelated (one queue), so an iid
    bootstrap would understate the uncertainty. Resampling blocks keeps that structure.
    """
    def prep(x, blocks):
        order = np.argsort(blocks, kind="stable")
        xs, bs = x[order], blocks[order]
        uniq, starts, counts = np.unique(bs, return_index=True, return_counts=True)
        return xs, starts, counts

    def resample(xs, starts, counts):
        pick = rng.integers(0, len(starts), len(starts))
        lens = counts[pick]
        total = lens.sum()
        # vectorised concatenation of ranges [starts[pick], starts[pick]+lens)
        offsets = np.repeat(starts[pick] - np.r_[0, np.cumsum(lens)[:-1]], lens)
        return xs[np.arange(total) + offsets]

    p1, p2 = prep(x1, blocks1), prep(x2, blocks2)
    diffs = np.array([np.quantile(resample(*p2), q) - np.quantile(resample(*p1), q)
                      for _ in range(n_boot)])
    return np.quantile(diffs, [0.025, 0.975])


def part_d_upgrade(merged):
    print("\n=== Part D: did the line-B upgrade help? ===")
    d1, d2 = sorted(merged["date"].unique())
    b1 = merged.loc[(merged["date"] == d1), "lat_B"].dropna()
    b2 = merged.loc[(merged["date"] == d2), "lat_B"].dropna()
    a1 = merged.loc[(merged["date"] == d1), "lat_A"].dropna()
    a2 = merged.loc[(merged["date"] == d2), "lat_A"].dropna()

    naive = pd.DataFrame({
        "B day1": np.percentile(b1, PCTS), "B day2": np.percentile(b2, PCTS),
        "A day1": np.percentile(a1, PCTS), "A day2": np.percentile(a2, PCTS)},
        index=[f"p{p:g}" for p in PCTS]).round(2)
    naive["B change"] = naive["B day2"] - naive["B day1"]
    naive["A change (control)"] = naive["A day2"] - naive["A day1"]
    print("Naive before/after (us):")
    print(naive.to_string())

    ks = stats.ks_2samp(b1, b2)
    print(f"\nKS test B day1 vs day2: D={ks.statistic:.3f}, p={ks.pvalue:.2e}  "
          "(with n~10^5 any difference is 'significant' -- report effect sizes, not p-values)")

    rng = np.random.default_rng(0)
    sec = lambda date: ((merged.loc[merged["date"] == date].dropna(subset=["lat_B"])["exch_ns"])
                        // 1_000_000_000).to_numpy()
    for q in (0.5, 0.99):
        lo, hi = block_bootstrap_diff(b1.to_numpy(), sec(d1), b2.to_numpy(), sec(d2), q, 200, rng)
        print(f"Block-bootstrap 95% CI for B quantile({q}) day2-day1: [{lo:+.2f}, {hi:+.2f}] us")

    # Stratify by message rate: compare like with like.
    print("\nMix of traffic by rate bucket (pct of messages):")
    print((pd.crosstab(merged["rate_bucket"], merged["date"], normalize="columns") * 100)
          .round(2).to_string())
    rows = []
    for bucket, g in merged.groupby("rate_bucket", observed=True):
        r = {"rate_bucket": str(bucket)}
        for col, name in (("lat_A", "A"), ("lat_B", "B")):
            for date, tag in ((d1, "d1"), (d2, "d2")):
                v = g.loc[g["date"] == date, col].dropna()
                r[f"{name}_{tag}_p50"] = v.median()
                r[f"{name}_{tag}_p99"] = v.quantile(0.99)
        r["n_d1"], r["n_d2"] = (g["date"] == d1).sum(), (g["date"] == d2).sum()
        rows.append(r)
    strat = pd.DataFrame(rows)
    strat["B_p50_change"] = strat["B_d2_p50"] - strat["B_d1_p50"]
    strat["B_p99_change"] = strat["B_d2_p99"] - strat["B_d1_p99"]
    strat["DiD_p50 (B chg - A chg)"] = strat["B_p50_change"] - (strat["A_d2_p50"] - strat["A_d1_p50"])
    print("\nRate-stratified comparison (us):")
    cols = ["rate_bucket", "n_d1", "n_d2", "B_d1_p50", "B_d2_p50", "B_p50_change",
            "B_d1_p99", "B_d2_p99", "B_p99_change", "DiD_p50 (B chg - A chg)"]
    print(strat[cols].round(2).to_string(index=False))
    print("Note: in the saturated buckets (>=100 msgs/100us) raw B day2 is still worse: the rate in\n"
          "the last 100us is an imperfect proxy for queue depth (day-2 bursts last longer), so\n"
          "stratification leaves residual confounding. Line A, which did not change, is the control.")

    # Best design: PAIRED comparison. Both lines carry the *same packets*, so per-packet
    # rx_B - rx_A cancels exchange behaviour, traffic mix, clock offset and most queueing.
    both = merged.dropna(subset=["lat_A", "lat_B"])
    paired = (both["lat_B"] - both["lat_A"]).groupby([both["rate_bucket"], both["date"]],
                                                     observed=True).median().unstack()
    paired["change"] = paired[d2] - paired[d1]
    print("\nPaired per-packet median(B - A) by rate bucket (us; <0 means B faster):")
    print(paired.round(2).to_string())
    overall = (both["lat_B"] - both["lat_A"]).groupby(both["date"]).median()
    print(f"Overall median(B - A): day1 {overall[d1]:+.2f} us -> day2 {overall[d2]:+.2f} us "
          f"=> B improved by ~{overall[d1] - overall[d2]:.1f} us relative to A")
    strat["paired_change"] = paired["change"].to_numpy()
    return strat


# ----------------------------------------------------------------------------- Part E
def part_e_microbursts(merged):
    print("\n=== Part E: microbursts and the capacity knee ===")
    for date, g in merged.groupby("date"):
        r = g["rate_100us"]
        print(f"{date}: rate(msgs/100us) p50={r.median():.0f} p99={r.quantile(.99):.0f} "
              f"max={r.max()}")

    # Use line A on both days (unchanged hardware) to locate the knee.
    g = merged.dropna(subset=["lat_A"])
    binned = g.groupby(np.minimum(g["rate_100us"] // 5 * 5, 250))["lat_A"].agg(
        ["median", lambda s: s.quantile(0.99), "size"])
    binned.columns = ["p50", "p99", "n"]
    binned = binned[binned["n"] >= 200]

    # Knee = first rate bin from which the median stays > 2 us above the idle baseline.
    # (A hinge/piecewise-linear fit also works, but queueing delay is convex in load, so a
    # straight-line model after the knee gets dragged right by the extreme bins.)
    baseline = binned.loc[binned.index < 20, "p50"].median()
    above = binned["p50"] > baseline + 2.0
    # all bins from here on are above -> reverse cumulative AND
    stays_above = above[::-1].cummin()[::-1]
    knee = float(stays_above[stays_above].index.min())
    print(f"Idle baseline median {baseline:.1f} us; knee (median > baseline + 2us from here on) "
          f"~ {knee:.0f} msgs/100us; at 150 msgs/100us median is "
          f"{binned['p50'].iloc[(np.abs(binned.index - 150)).argmin()]:.0f} us")
    mean_bytes = 40 + 20 + 62
    print(f"Sanity: 1 Gb/s with ~{mean_bytes} B on the wire per msg serialises ~"
          f"{100_000 / (mean_bytes * 8):.0f} msgs/100us -> knee at ~{knee / (100_000 / (mean_bytes * 8)):.0%} "
          "utilisation; delay ~ rho/(1-rho) explodes as rho -> 1")

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(binned.index, binned["p50"], "o-", ms=3, label="p50")
    ax.plot(binned.index, binned["p99"], "o-", ms=3, label="p99")
    ax.axvline(knee, ls="--", color="gray", label=f"fitted knee ~{knee:.0f}")
    ax.set(xlabel="msgs in previous 100us (exchange side)", ylabel="line A latency (us)",
           yscale="log", title="Latency vs instantaneous message rate")
    ax.legend()
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(PLOTS / "p04_latency_vs_rate.png", dpi=110)
    plt.close(fig)
    return knee


# ----------------------------------------------------------------------------- Part F
def part_f_summary(merged, strat, knee):
    print("\n=== Part F: summary for the network team ===")
    d1, d2 = sorted(merged["date"].unique())
    win = merged.loc[merged["winner"].isin(["A", "B"])].groupby("date")["winner"].apply(
        lambda s: (s == "B").mean() * 100)
    low = strat.iloc[:4]
    print(f"1. B now wins the A/B race on {win[d2]:.0f}% of packets (was {win[d1]:.0f}%).")
    print(f"2. Yes, the upgrade helped: on the SAME packets, B went from ~2.5 us slower than A to "
          f"~1 us faster (paired change {strat['paired_change'].median():+.1f} us, similar in every "
          f"load bucket); below the knee B's own median dropped {-low['B_p50_change'].mean():.1f} us.")
    print("3. The naive day-over-day p99 got WORSE on both lines because day 2 had ~3x more "
          "messages in microbursts; that is traffic mix (Simpson's paradox), not the switch.")
    print(f"4. Both lines hit a capacity knee at ~{knee:.0f} msgs/100us (~75% of the 1G cross-connect's "
          "~100 msgs/100us); above it queueing dominates p99. Fix = more bandwidth, not a faster switch.")
    print("5. B drops packets in clusters during microbursts (shallow buffer) and a few seqs "
          "were lost on both lines (upstream) -> need retransmission/snapshot; absolute one-way "
          "latency carries a drifting clock offset (~2 us over 2 h) -- compare lines, not absolutes.")

    # The one chart: per-load-level change, B vs A (control), plus the paired change.
    fig, ax = plt.subplots(figsize=(9, 4.5))
    xs = np.arange(len(strat))
    ax.bar(xs - 0.27, strat["B_d2_p50"] - strat["B_d1_p50"], 0.27, label="line B p50 change")
    ax.bar(xs, strat["A_d2_p50"] - strat["A_d1_p50"], 0.27, label="line A p50 change (control)")
    ax.bar(xs + 0.27, strat["paired_change"], 0.27, label="paired median(B-A) change")
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks(xs, strat["rate_bucket"], rotation=30)
    ax.set(xlabel="msgs in previous 100us", ylabel="day2 - day1 (us)",
           title="At every load level B improved relative to A (paired change < 0)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "p04_upgrade_by_rate.png", dpi=110)
    plt.close(fig)


def main():
    t0 = time.time()
    A, B = part_a_load_and_check()
    part_b_latency(A, B)
    merged = part_c_arbitration(A, B)
    merged = add_rate(merged)
    strat = part_d_upgrade(merged)
    knee = part_e_microbursts(merged)
    part_f_summary(merged, strat, knee)
    print(f"\nTotal runtime {time.time() - t0:.1f}s; plots in {PLOTS}")


if __name__ == "__main__":
    main()
