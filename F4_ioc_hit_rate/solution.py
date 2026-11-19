"""F4: IOC take-attempt hit rate vs our latency (5 venues, 2 strategies, 20 days). Run make_data.py first."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA, PLOTS = ROOT / "data" / "p05" / "take_attempts.parquet", ROOT / "plots"
BREAK_Z = 4      # DECISION: not 1.96 -- 95 day x venue tests would give ~5 false alarms by chance alone
RELEASE_DAY, NEW_REGIME_DAY = 11, 16      # set AFTER step 2 found the breaks
YLABEL = "hit rate (fraction of IOCs filled)"
plt.rcParams.update({"axes.grid": True, "axes.grid.axis": "y", "grid.alpha": 0.3, "savefig.dpi": 110})

def ci95(k, n):
    # DECISION: normal approx, fine while n*p, n*(1-p) >= ~10. LIMITATION: assumes independent attempts
    p = k / n
    half = 1.96 * np.sqrt(p * (1 - p) / n)
    return p - half, p + half

def load(path=DATA):
    raw = pd.read_parquet(path)
    df = raw.drop_duplicates().copy()   # DECISION: byte-identical rows = re-sent capture records -> drop
    assert df.attempt_id.is_unique      # CHECK: no "same id, different content" left behind
    assert ((df.outcome == "MISSED") == (df.filled_qty == 0)).all()   # CHECK: outcome agrees with fills
    r = df.reaction_ns.astype("float64")  # nullable Int64 -> float, <NA> -> NaN
    print(f"== STEP 1  data quality: rows={len(raw):,}  exact duplicates={len(raw) - len(df)}  "
          f"reaction missing={r.isna().sum()}  reaction<=0={(r <= 0).sum()}")
    # DECISION: bad latency -> keep the row (its fill outcome is real), blank only the latency
    df["reaction_us"] = r.where(r > 0) / 1e3
    df["day"] = df.date.rank(method="dense").astype(int)   # trading day 1..20 (weekends skipped)
    df["hit"] = df.filled_qty > 0       # DECISION: any fill = we won the race (partials count)
    return df

def step1_hit_rates(df, by):
    g = df.groupby(by).agg(n=("hit", "size"), k=("hit", "sum"),
                           filled=("filled_qty", "sum"), target=("target_qty", "sum"))
    g["hit_rate"] = g.k / g.n
    g["ci_lo"], g["ci_hi"] = ci95(g.k, g.n)
    g["qty_rate"] = g.filled / g.target   # quantity-based: share of targeted contracts filled
    return g[["n", "hit_rate", "ci_lo", "ci_hi", "qty_rate"]].round(3)

def step2_stability(df):
    g = df.groupby(["day", "venue"]).hit.agg(["sum", "size"])
    k, n = g["sum"].unstack(), g["size"].unstack()
    rate, var = k / n, k / n * (1 - k / n) / n
    z = rate.diff() / np.sqrt(var + var.shift())   # two-sample z-test: day d vs day d-1, per venue
    breaks = pd.DataFrame({"before": rate.shift().stack(), "after": rate.stack(), "z": z.stack()})
    fig, ax = plt.subplots(figsize=(10, 4.5), layout="constrained")
    for v in rate.columns:
        ax.plot(rate.index, rate[v], marker="o", ms=4, label=v)
        ax.fill_between(rate.index, *ci95(k[v], n[v]), alpha=0.2)
    ax.set(xticks=rate.index, xlabel="trading day (1 = 2026-10-19, 20 = 2026-11-13)", ylabel=YLABEL,
           title="EX1/EX2 jump on day 11, EX3 collapses on day 16 (bands = 95% CI)")
    ax.legend(ncol=5, loc="lower left")
    fig.savefig(PLOTS / "f4_daily_hit_rate.png")
    # CHECK: "us or them?" -- did OUR latency move at the break? did both strategies move together?
    d = df[df.venue.isin(["EX1", "EX3"])]
    period = pd.cut(d.day, [0, RELEASE_DAY - 1, NEW_REGIME_DAY - 1, 20], labels=["d1-10", "d11-15", "d16-20"])
    t = d.groupby(["venue", "strategy", period]).agg(hit=("hit", "mean"), us=("reaction_us", "median"))
    return breaks[breaks.z.abs() > BREAK_Z].round(2), t.unstack().round(2)

def step3_race_curve(df, n_bins=10):
    d = df[(df.day < NEW_REGIME_DAY) & df.reaction_us.notna()].copy()   # DECISION: stable window, pre-EX3 break
    # DECISION: quantile bins per venue -> equal n per bin; equal-width bins leave the tails nearly empty
    d["bin"] = d.groupby("venue").reaction_us.transform(lambda s: pd.qcut(s, n_bins, labels=False))
    c = d.groupby(["venue", "bin"]).agg(us=("reaction_us", "median"), hit=("hit", "mean"), n=("hit", "size"))
    fig, ax = plt.subplots(figsize=(9, 4.5), layout="constrained")
    for v, g in c.groupby(level="venue"):   # LIMITATION: pools S1+S2 and all edge sizes -> not causal
        ax.errorbar(g.us, g.hit, yerr=1.96 * np.sqrt(g.hit * (1 - g.hit) / g.n), marker="o", ms=4, capsize=2, label=v)
    ax.set_xscale("log")
    ax.set_xticks([0.8, 1, 1.5, 2, 3, 4], ["0.8", "1", "1.5", "2", "3", "4"])
    ax.minorticks_off()                 # otherwise log minor ticks get "6x10^-1"-style labels
    ax.set(xlabel="our reaction time, µs (decile median, log scale)", ylabel=YLABEL,
           title="Slower reaction, lower hit rate on every venue; EX1 is the most contested (days 1-15)")
    ax.legend()
    fig.savefig(PLOTS / "f4_race_curve.png")
    ends = c.groupby(level="venue")[["us", "hit"]].agg(["first", "last"])   # fastest vs slowest decile
    assert (ends["hit"]["first"] > ends["hit"]["last"]).all()   # CHECK: slower -> lower hit, every venue
    return ends.round(2)

def step4_s1_vs_s2(df):
    d = df[df.day < RELEASE_DAY]        # DECISION: before the S1 release, so neither strategy changed
    by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
    mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
    pooled = (by_venue * mix).sum()     # = the raw pooled rate: venue rates weighted by each strategy's OWN mix
    # DECISION: direct standardization -- weight both by S1's venue mix: same opportunities, other strategy
    same_mix = by_venue.mul(mix.S1, axis=0).sum()
    fig, ax = plt.subplots(figsize=(9, 4), layout="constrained")
    bars = pd.concat([by_venue, pooled.to_frame("ALL (pooled)").T])
    bars.plot.bar(ax=ax, rot=0, xlabel="venue", ylabel=YLABEL, ylim=(0, 1.15))
    ax.set_title("S1 beats S2 on every venue; S2 wins pooled only via its EX5-heavy mix (days 1-10)")
    fig.savefig(PLOTS / "f4_s1_vs_s2.png")
    return by_venue.join(mix, rsuffix="_venue_mix").round(3), pooled, same_mix

def main():
    PLOTS.mkdir(exist_ok=True)
    df = load()
    rates = pd.concat([step1_hit_rates(df, "venue"), step1_hit_rates(df, "strategy")])
    print(f"== STEP 1  hit rate by venue / strategy, all 20 days (95% CI)\n{rates}")
    breaks, us_or_them = step2_stability(df)
    print(f"\n== STEP 2  day-over-day breaks (|z| > {BREAK_Z}), then 'us or them?'\n{breaks}\n{us_or_them}")
    print(f"\n== STEP 3  race curve: fastest (first) vs slowest (last) decile, days 1-15\n{step3_race_curve(df)}")
    table, pooled, same_mix = step4_s1_vs_s2(df)
    print(f"\n== STEP 4  S1 vs S2, days 1-10\n{table}")
    print(f"pooled: S1={pooled.S1:.3f} S2={pooled.S2:.3f} | on S1's mix: S1={same_mix.S1:.3f} S2={same_mix.S2:.3f}")
    print(f"""
CONCLUSION
- Hit rate is a latency race: on every venue it falls as our reaction slows; EX1 is the most contested.
- Breaks: day 11 EX1/EX2 up = our S1 release; day 16 EX3 falls for both strategies with our latency flat = not us.
- 'S2 is better' is Simpson's paradox: S1 wins every venue; on S1's mix S2 hits {same_mix.S2:.0%} vs {same_mix.S1:.0%}.
LIMITATIONS
- Observational: slow bins are mostly S2 (smaller edges, less competition), so the race curve is not causal.
- Hit rate ignores fill size, edge per fill and adverse selection: more fills is not more P&L.
- Competitors change (EX3, day 16), so curves expire; only 20 days, and same-day attempts are correlated.""")

if __name__ == "__main__":
    main()
