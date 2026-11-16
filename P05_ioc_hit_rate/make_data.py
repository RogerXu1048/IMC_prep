"""Generate synthetic IOC take-attempt data for P05 (and P06).

Usage:  python3 make_data.py
Output: ../data/p05/take_attempts.parquet

Everything is simulated from a seeded RNG, so every run produces the same file.
Do not read this file before attempting the problem -- it contains the
generative model (i.e. the answers).
"""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20261019
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "p05"

VENUES = np.array(["EX1", "EX2", "EX3", "EX4", "EX5"])
# Fastest-competitor latency (signal -> their order at the matching engine), lognormal.
COMP_MEDIAN_US = np.array([1.6, 2.2, 2.8, 3.5, 5.0])
COMP_SIGMA = 0.35
EDGE_COMPETITION = 0.07            # each extra tick of edge -> competition ~7% faster
FADE_PROB = np.array([0.08, 0.10, 0.12, 0.10, 0.06])   # quote cancelled before we arrive
JITTER_US = 0.12                   # exchange-side arrival jitter (both us and them)

# Strategy -> venue mix. S1 is the fast, broad strategy; S2 is slow, lives on EX5.
S1_MIX_BEFORE = np.array([0.42, 0.30, 0.14, 0.09, 0.05])
S1_MIX_AFTER = np.array([0.50, 0.27, 0.12, 0.07, 0.04])  # after release: more EX1
S2_MIX = np.array([0.01, 0.02, 0.04, 0.08, 0.85])
S1_SHARE = 0.72

S1_MEDIAN_BEFORE_US, S1_MEDIAN_AFTER_US, S1_SIGMA = 1.40, 1.10, 0.22
S2_MEDIAN_US, S2_SIGMA = 3.20, 0.20

RELEASE_DAY = 11                   # S1 code path ~300 ns faster from this day
VOLATILE_DAYS = range(11, 16)      # market-wide: more quote fading
NEW_COMPETITOR_DAY = 16            # new fast competitor on EX3
NEW_COMP_MEDIAN_US, NEW_COMP_SIGMA = 1.05, 0.20

UNDERLYINGS = np.array([
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "AVGO",
    "JPM", "BAC", "XOM", "CVX", "PFE", "MRK", "KO", "PEP", "WMT", "COST",
    "DIS", "INTC", "CSCO", "ORCL", "CRM", "UBER", "SPY", "QQQ", "IWM", "SMCI",
])


def intraday_offsets_ns(rng, n):
    """U-shaped intraday activity: busy at the open and the close."""
    minutes = np.arange(390)
    weight = 1.0 + 2.5 * np.exp(-minutes / 25.0) + 1.5 * np.exp(-(389 - minutes) / 25.0)
    minute = rng.choice(minutes, size=n, p=weight / weight.sum())
    seconds = minute * 60 + rng.random(n) * 60
    return (seconds * 1e9).astype(np.int64)


def simulate_day(rng, day_idx, date):
    n = int(rng.poisson(8500 * (1.15 if day_idx in VOLATILE_DAYS else 1.0)))
    is_s1 = rng.random(n) < S1_SHARE
    strategy = np.where(is_s1, "S1", "S2")

    s1_mix = S1_MIX_AFTER if day_idx >= RELEASE_DAY else S1_MIX_BEFORE
    venue_idx = np.where(is_s1,
                         rng.choice(5, size=n, p=s1_mix),
                         rng.choice(5, size=n, p=S2_MIX))

    # S1 chases bigger edges (and therefore more competition) than S2.
    edge = 1 + np.where(is_s1, rng.poisson(2.0, n), rng.poisson(1.0, n))
    spread = 1 + rng.poisson(2.0, n) + (edge > 3)
    qty = np.minimum(1 + rng.geometric(0.12, n), 100)

    s1_median = S1_MEDIAN_AFTER_US if day_idx >= RELEASE_DAY else S1_MEDIAN_BEFORE_US
    reaction_us = np.where(
        is_s1,
        s1_median * np.exp(rng.normal(0, S1_SIGMA, n)),
        S2_MEDIAN_US * np.exp(rng.normal(0, S2_SIGMA, n)),
    )

    comp_us = (COMP_MEDIAN_US[venue_idx] * np.exp(-EDGE_COMPETITION * (edge - 2))
               * np.exp(rng.normal(0, COMP_SIGMA, n)))
    if day_idx >= NEW_COMPETITOR_DAY:
        newcomer = NEW_COMP_MEDIAN_US * np.exp(rng.normal(0, NEW_COMP_SIGMA, n))
        comp_us = np.where(venue_idx == 2, np.minimum(comp_us, newcomer), comp_us)

    fade_p = FADE_PROB[venue_idx] + (0.03 if day_idx in VOLATILE_DAYS else 0.0)
    faded = rng.random(n) < fade_p
    we_arrive_first = (reaction_us + rng.normal(0, JITTER_US, n)
                       < comp_us + rng.normal(0, JITTER_US, n))
    win = we_arrive_first & ~faded

    # Partial fills: occasionally someone else takes part of the size either way.
    can_partial = qty >= 2
    partial_after_win = win & can_partial & (rng.random(n) < 0.05)
    partial_after_loss = ~win & ~faded & can_partial & (rng.random(n) < 0.025)
    partial_qty = 1 + np.floor(rng.random(n) * np.maximum(qty - 1, 1)).astype(np.int64)
    filled = np.where(win, qty, 0)
    filled = np.where(partial_after_win | partial_after_loss, partial_qty, filled)

    outcome = np.where(filled == qty, "FILLED", np.where(filled > 0, "PARTIAL", "MISSED"))

    # Exchange response time: a match takes longer than "IOC cancelled, nothing to do".
    # This is known only AFTER the outcome -> leaky as a model feature.
    resp_us = np.where(filled > 0,
                       31.0 * np.exp(rng.normal(0, 0.18, n)),
                       21.0 * np.exp(rng.normal(0, 0.20, n)))

    session_open_utc = (pd.Timestamp(f"{date} 09:30", tz="America/New_York")
                        .tz_convert("UTC").value)
    ts_ns = session_open_utc + intraday_offsets_ns(rng, n)

    return pd.DataFrame({
        "date": pd.Timestamp(date),
        "ts_ns": ts_ns,
        "venue": VENUES[venue_idx],
        "strategy": strategy,
        "underlying": rng.choice(UNDERLYINGS, size=n,
                                 p=(w := 1 / np.arange(1, 31) ** 0.8) / w.sum()),
        "edge_ticks": edge.astype(np.int64),
        "spread_ticks": spread.astype(np.int64),
        "target_qty": qty.astype(np.int64),
        "reaction_ns": np.round(reaction_us * 1000).astype(np.int64),
        "resp_ns": np.round(resp_us * 1000).astype(np.int64),
        "filled_qty": filled.astype(np.int64),
        "outcome": outcome,
    })


def add_messiness(rng, df):
    """Things real capture data has: missing timestamps, clock steps, duplicate records."""
    n = len(df)
    reaction = df["reaction_ns"].astype("Int64")
    missing = rng.random(n) < 0.001          # hardware timestamp missing
    reaction[missing] = pd.NA
    clock_step = rng.random(n) < 0.0004      # NIC clock stepped -> negative "latency"
    reaction[clock_step] = -reaction[clock_step]
    df["reaction_ns"] = reaction

    dup_idx = rng.choice(n, size=int(n * 0.001), replace=False)  # re-sent capture records
    df = pd.concat([df, df.iloc[dup_idx]], ignore_index=True)
    return df.sort_values("ts_ns", kind="stable").reset_index(drop=True)


def main():
    rng = np.random.default_rng(SEED)
    dates = pd.bdate_range("2026-10-19", periods=20)
    frames = [simulate_day(rng, i + 1, d.strftime("%Y-%m-%d")) for i, d in enumerate(dates)]
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("ts_ns", kind="stable").reset_index(drop=True)
    df.insert(0, "attempt_id", np.arange(1, len(df) + 1, dtype=np.int64) + 7_000_000)
    df = add_messiness(rng, df)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "take_attempts.parquet"
    df.to_parquet(path, index=False)
    print(f"wrote {path}  ({len(df):,} rows, {path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
