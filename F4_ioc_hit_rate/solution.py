"""F4 -- IOC hit rate vs latency: what is 200 ns worth?  (run make_data.py first)"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p05" / "take_attempts.parquet"
PLOTS = ROOT / "plots"
VENUES = ["EX1", "EX2", "EX3", "EX4", "EX5"]
Z95 = 1.96
BREAK_Z = 5.0                 # a day-over-day move must exceed 5 standard errors to count as a break
STABLE_DAYS = range(1, 16)    # set after step 2: day 16 starts a new regime on EX3
SPEEDUP_US = 0.2
USD_PER_TICK_CONTRACT = 0.01 * 100   # $0.01 tick x 100 contract multiplier


def wilson_ci(k, n, z: float = Z95):
    # DECISION: Wilson score interval, not p +/- z*sqrt(p(1-p)/n): it never leaves [0, 1]
    # and keeps ~95% coverage when n is small or p is near 0/1 (daily x venue cells).
    k, n = np.asarray(k, float), np.asarray(n, float)
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return centre - half, centre + half


def rate_table(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by).agg(n=("hit", "size"), k=("hit", "sum"),
                           target=("target_qty", "sum"), filled=("filled_qty", "sum"))
    g["hit_rate"] = g.k / g.n
    g["ci_lo"], g["ci_hi"] = wilson_ci(g.k, g.n)
    g["qty_rate"] = g.filled / g.target
    return g[["n", "hit_rate", "ci_lo", "ci_hi", "qty_rate"]].round(3)


def step1_load_and_define(path: Path = DATA) -> tuple[pd.DataFrame, dict]:
    raw = pd.read_parquet(path)
    df = raw.drop_duplicates().copy()            # re-sent capture records are byte-identical
    assert df.attempt_id.is_unique               # CHECK: no "same id, different content"
    df["day"] = df.date.rank(method="dense").astype(int)
    # DECISION: count-based hit = any fill (we won the race); a partial still means we arrived first.
    df["hit"] = df.filled_qty > 0
    # DECISION: missing / non-positive latency (NIC clock step) -> keep the row for hit-rate
    # tables (its outcome is real) but exclude it from anything that uses reaction time.
    df["latency_ok"] = (df.reaction_ns.notna() & (df.reaction_ns > 0)).fillna(False).astype(bool)
    df["reaction_us"] = df.reaction_ns.astype("float64") / 1e3
    quality = {"raw_rows": len(raw), "duplicates": len(raw) - len(df),
               "reaction_missing": int(df.reaction_ns.isna().sum()),
               "reaction_nonpositive": int((df.reaction_ns <= 0).sum())}
    return df, quality


def step2_daily_breaks(df: pd.DataFrame) -> pd.Series:
    daily = df.groupby(["day", "venue"]).agg(n=("hit", "size"), k=("hit", "sum"))
    rate = (daily.k / daily.n).unstack()
    var = rate * (1 - rate) / daily.n.unstack()
    # two-sample z-test, day d vs day d-1, per venue
    # TRADE-OFF: assumes attempts are independent; they are not (intraday shocks), so z is
    # inflated -> use a high threshold (5) or a day-block bootstrap if it matters.
    z = rate.diff() / np.sqrt(var + var.shift())
    breaks = z.stack()[lambda s: s.abs() > BREAK_Z]

    dates = df.groupby("day").date.first().dt.strftime("%m-%d")
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for v in VENUES:
        lo, hi = wilson_ci(daily.k.xs(v, level="venue"), daily.n.xs(v, level="venue"))
        ax.plot(rate.index, rate[v], marker="o", label=v)
        ax.fill_between(rate.index, lo, hi, alpha=0.15)
    ax.set_xticks(rate.index, dates.values, rotation=60)
    ax.set(ylabel="hit rate", title="Daily IOC hit rate by venue (95% Wilson CI)")
    ax.legend(ncol=5)
    fig.tight_layout()
    fig.savefig(PLOTS / "f4_daily_hit_rate.png", dpi=110)
    plt.close(fig)
    return pd.DataFrame({"before": [rate.loc[d - 1, v] for d, v in breaks.index],
                         "after": [rate.loc[d, v] for d, v in breaks.index],
                         "z": breaks.values}, index=breaks.index).round(3)


def step3_race_curve(df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    # DECISION: stable window only (days 1-15) -> the EX3 regime change can't bend the curve.
    d = df[df.day.isin(STABLE_DAYS) & df.latency_ok].copy()
    # DECISION: quantile bins per venue -> equal n per bin -> comparable CIs.
    # TRADE-OFF: equal-width bins are easier to read but leave the tails nearly empty.
    d["bin"] = d.groupby("venue").reaction_us.transform(lambda s: pd.qcut(s, n_bins, labels=False))
    curve = d.groupby(["venue", "bin"]).agg(t_us=("reaction_us", "median"),
                                            n=("hit", "size"), k=("hit", "sum"))
    curve["rate"] = curve.k / curve.n
    curve["lo"], curve["hi"] = wilson_ci(curve.k, curve.n)
    ends = curve.groupby("venue").rate.agg(["first", "last"])
    assert (ends["first"] > ends["last"]).all()   # CHECK: slower -> lower hit rate on every venue

    fig, ax = plt.subplots(figsize=(9, 4.5))
    for v, c in curve.groupby(level="venue"):
        ax.errorbar(c.t_us, c.rate, yerr=[c.rate - c.lo, c.hi - c.rate], marker="o", capsize=3, label=v)
    ax.set_xscale("log")
    ax.set(xlabel="our reaction time, us (bin median, log scale)", ylabel="hit rate",
           title="Race curve: hit rate vs our reaction time (days 1-15, decile bins)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "f4_race_curve.png", dpi=110)
    plt.close(fig)
    return curve


def step4_simpson(df: pd.DataFrame) -> dict:
    d = df[df.day <= 10]                          # before the S1 release: neither strategy changed
    by_venue = d.groupby(["venue", "strategy"]).hit.mean().unstack()
    mix = pd.crosstab(d.venue, d.strategy, normalize="columns")
    # DECISION: direct standardization -- score both strategies on ONE venue mix (S1's),
    # so the comparison is "same opportunities, different strategy".
    standardized = by_venue.mul(mix["S1"], axis=0).sum()
    return {"overall": d.groupby("strategy").hit.mean().round(3),
            "median_reaction_us": d[d.latency_ok].groupby("strategy").reaction_us.median().round(2),
            "by_venue": by_venue.round(3), "venue_mix": mix.round(3),
            "standardized_on_S1_mix": standardized.round(3)}


def step5_value_of_speed(df: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    # DECISION: price the speed-up on days 11-15 = today's latency profile (post-release)
    # but still before the EX3 break, using the race curve measured on the stable window.
    d = df[df.day.between(11, 15) & df.latency_ok].copy()
    p_now, p_fast = np.zeros(len(d)), np.zeros(len(d))
    for v, c in curve.groupby(level="venue"):
        m = (d.venue == v).to_numpy()
        t = d.reaction_us.to_numpy()[m]
        # DECISION: piecewise-linear interpolation of the empirical curve (no model).
        # TRADE-OFF: np.interp is flat outside the observed range -> no extrapolation,
        # so the fastest attempts get no credit (conservative); a fitted model could extrapolate.
        p_now[m] = np.interp(t, c.t_us, c.rate)
        p_fast[m] = np.interp(t - SPEEDUP_US, c.t_us, c.rate)
    fill_frac = (df.filled_qty[df.hit] / df.target_qty[df.hit]).mean()   # partial fills
    d["extra_fills"] = p_fast - p_now
    d["extra_usd"] = d.extra_fills * d.target_qty * fill_frac * d.edge_ticks * USD_PER_TICK_CONTRACT
    per_day = d.groupby("venue")[["extra_fills", "extra_usd"]].sum() / d.day.nunique()
    per_day.loc["TOTAL"] = per_day.sum()
    return per_day.round(0)


def main() -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    df, quality = step1_load_and_define()
    print("== Step 1: data quality", quality)
    print(rate_table(df, "venue"), "\n", rate_table(df, "strategy"))

    print("\n== Step 2: day-over-day breaks (|z| > 5)")
    print(step2_daily_breaks(df))

    print("\n== Step 3: race curve, hit rate by reaction-time decile (days 1-15)")
    curve = step3_race_curve(df)
    print(curve.rate.unstack("venue").round(2))

    print("\n== Step 4: S1 vs S2 (days 1-10)")
    for name, value in step4_simpson(df).items():
        print(f"-- {name}\n{value}")

    print(f"\n== Step 5: value of {SPEEDUP_US * 1000:.0f} ns faster (days 11-15 attempt mix, per day)")
    print(step5_value_of_speed(df, curve))
    print("assumes: full edge captured, no adverse selection, competitors don't react")


if __name__ == "__main__":
    main()
