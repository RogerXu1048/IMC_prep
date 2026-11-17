"""Generate data for F6: options market-making quotes, underlying ticks and our fills (1 hour).

Writes ../data/f6/: underlying_ticks.parquet, our_quotes.parquet, our_fills.parquet, series.csv,
and _truth.parquet (self-check only; don't open it while solving).
"""
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data" / "f6"
SEED = 606
T0 = pd.Timestamp("2026-11-12 14:30:00", tz="UTC").value      # epoch ns
DURATION_S = 3600
UNDERLYINGS = {"AAPL": 230.00, "NVDA": 180.00, "TSLA": 310.00}
VENUE_BASE_US = {"EX1": 2.5, "EX2": 3.5, "EX3": 7.0}          # requote path latency per venue
PER_SERIES_US = 0.5            # engine sends requotes one series after another
N_STRIKES = 20
HALF_SPREAD = 0.02
TICK_RATE = 0.4                # underlying mid changes per second
JUMP_SIZES, JUMP_P = [0.01, 0.02, 0.05, 0.10], [0.55, 0.25, 0.12, 0.08]
TAKER_MEDIAN_US, TAKER_SIGMA = 9.0, 0.3
P_HUNT = 0.05                  # chance a fast taker goes after a stale, mispriced quote
UNINFORMED_PER_S = 10.0


def make_series(rng):
    rows = []
    for und, spot in UNDERLYINGS.items():
        for k in np.round(spot * (1 + np.linspace(-0.10, 0.10, N_STRIKES))):
            call_delta = float(np.clip(0.5 - (k / spot - 1) * 4.5, 0.05, 0.95))
            for cp, delta in (("C", call_delta), ("P", call_delta - 1)):
                intrinsic = max(spot - k, 0) if cp == "C" else max(k - spot, 0)
                rows.append({"series_id": f"{und}-{cp}-{k:.0f}", "underlying": und, "cp": cp,
                             "strike": k, "delta": round(delta, 3),
                             "ref_fair": round(intrinsic + rng.uniform(1.5, 4.0), 2)})
    s = pd.DataFrame(rows).sort_values("series_id").reset_index(drop=True)
    s["engine_pos"] = s.groupby("underlying").cumcount()        # requote order = series_id order
    return s


def main():
    rng = np.random.default_rng(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    series = make_series(rng)
    tick_rows, quote_frames, fill_frames, truth = [], [], [], []

    for und, spot in UNDERLYINGS.items():
        n = rng.poisson(TICK_RATE * DURATION_S)
        tick_ts = T0 + np.sort(rng.integers(1_000_000, DURATION_S * 10**9, n))
        jumps = rng.choice(JUMP_SIZES, n, p=JUMP_P) * rng.choice([-1, 1], n)
        mids = np.round(spot + np.cumsum(jumps), 2)
        tick_rows.append(pd.DataFrame({"ts_ns": tick_ts, "underlying": und, "mid": mids}))
        all_mids = np.concatenate([[spot], mids])                         # index 0 = before first tick

        s = series[series.underlying == und]
        for venue, base in VENUE_BASE_US.items():
            # fair value and quotes after each tick: shape (n_ticks + 1, n_series)
            fair = np.round(s.ref_fair.to_numpy() + np.outer(all_mids - spot, s.delta.to_numpy()), 2)
            bid, ask = np.round(fair - HALF_SPREAD, 2), np.round(fair + HALF_SPREAD, 2)
            lat_us = base + PER_SERIES_US * s.engine_pos.to_numpy() + rng.exponential(0.4, (n, len(s)))
            live = np.vstack([np.full(len(s), T0 - 1_000_000),
                              tick_ts[:, None] + (lat_us * 1000).astype("int64")])
            size = rng.integers(10, 51, len(s))
            for j, sid in enumerate(s.series_id):
                quote_frames.append(pd.DataFrame({"ts_ns": live[:, j], "series_id": sid, "venue": venue,
                                                  "bid": bid[:, j], "ask": ask[:, j], "size": size[j]}))
            # pick-offs: after tick k the old quote (row k) may be through the new fair (row k+1)
            through_ask = fair[1:] - ask[:-1]             # >0: stale ask too cheap -> we SELL
            through_bid = bid[:-1] - fair[1:]             # >0: stale bid too rich  -> we BUY
            exposed = (through_ask > 0) | (through_bid > 0)
            hunt = exposed & (rng.random(exposed.shape) < P_HUNT)
            taker_us = TAKER_MEDIAN_US * np.exp(TAKER_SIGMA * rng.standard_normal(exposed.shape))
            picked = hunt & (taker_us < lat_us)
            k_idx, j_idx = np.nonzero(picked)
            we_sell = through_ask[k_idx, j_idx] > 0
            fill_frames.append(pd.DataFrame({
                "ts_ns": tick_ts[k_idx] + (taker_us[k_idx, j_idx] * 1000).astype("int64"),
                "series_id": s.series_id.to_numpy()[j_idx], "venue": venue,
                "side": np.where(we_sell, "S", "B"),
                "price": np.where(we_sell, ask[k_idx, j_idx], bid[k_idx, j_idx]),
                "qty": size[j_idx], "_pickoff": True}))

            # uninformed customer flow at random times, at whatever quote is live
            m = rng.poisson(UNINFORMED_PER_S * DURATION_S / (len(UNDERLYINGS) * len(VENUE_BASE_US)))
            t = T0 + rng.integers(0, DURATION_S * 10**9, m)
            j = rng.integers(0, len(s), m)
            row = np.array([np.searchsorted(live[:, jj], tt, side="right") - 1 for tt, jj in zip(t, j)])
            # ~20% of customer flow is mildly informed: it trades in the direction of the NEXT move
            nxt = np.minimum(np.searchsorted(tick_ts, t, side="right"), n - 1)
            direction = np.sign(s.delta.to_numpy()[j] * jumps[nxt])
            informed = rng.random(m) < 0.20
            we_sell = np.where(informed, direction > 0, rng.random(m) < 0.5)
            fill_frames.append(pd.DataFrame({
                "ts_ns": t, "series_id": s.series_id.to_numpy()[j], "venue": venue,
                "side": np.where(we_sell, "S", "B"),
                "price": np.where(we_sell, ask[row, j], bid[row, j]),
                "qty": np.minimum(rng.geometric(0.2, m), size[j]), "_pickoff": False}))

    ticks = pd.concat(tick_rows).sort_values("ts_ns").reset_index(drop=True)
    quotes = pd.concat(quote_frames, ignore_index=True)
    fills = pd.concat(fill_frames, ignore_index=True).sort_values("ts_ns").reset_index(drop=True)
    fills.insert(0, "fill_id", [f"F{i:06d}" for i in range(len(fills))])
    fills[["fill_id", "_pickoff"]].rename(columns={"_pickoff": "is_pickoff"}).to_parquet(OUT / "_truth.parquet")
    fills = fills.drop(columns="_pickoff")

    # --- messiness ---------------------------------------------------------------------
    dup = fills.sample(frac=0.005, random_state=1)                  # exec reports re-sent on reconnect
    stray = fills.sample(15, random_state=2).assign(series_id="AMD-C-140")   # another desk's booking
    stray["fill_id"] = [f"X{i:05d}" for i in range(len(stray))]
    fills = pd.concat([fills, dup, stray]).sort_values("ts_ns", kind="stable").reset_index(drop=True)
    quotes = quotes.sort_values(["series_id", "venue", "ts_ns"]).reset_index(drop=True)  # warehouse order

    ticks.to_parquet(OUT / "underlying_ticks.parquet", index=False)
    quotes.to_parquet(OUT / "our_quotes.parquet", index=False)
    fills.to_parquet(OUT / "our_fills.parquet", index=False)
    series.drop(columns=["ref_fair", "engine_pos"]).to_csv(OUT / "series.csv", index=False)
    for f in sorted(OUT.iterdir()):
        print(f"{f.name:28s} {f.stat().st_size / 1e6:6.2f} MB")


if __name__ == "__main__":
    main()
