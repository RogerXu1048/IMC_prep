"""F6 -- quote pick-offs: markouts, stale fills, requote latency, and what a fix is worth."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA, PLOTS = HERE.parent / "data" / "f6", HERE.parent / "plots"
MULT = 100                                          # option contract multiplier
STREAM = ["series_id", "venue"]                     # one quote stream = one series on one venue
HORIZONS_US = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000]
LAT_BIN_US = 2


def step1_load_and_audit():
    ticks = pd.read_parquet(DATA / "underlying_ticks.parquet")
    quotes = pd.read_parquet(DATA / "our_quotes.parquet")
    fills = pd.read_parquet(DATA / "our_fills.parquet")
    series = pd.read_csv(DATA / "series.csv").sort_values("series_id")
    dq = {"dup_fills": int(fills.duplicated("fill_id").sum())}
    fills = fills.drop_duplicates("fill_id")          # DECISION: fill_id is the business key
    unknown = ~fills.series_id.isin(series.series_id)
    dq["unknown_series_fills"] = int(unknown.sum())   # TRADE-OFF: dropped, but reported, not ignored
    fills = fills[~unknown]
    dq["quotes_time_sorted"] = bool(quotes.ts_ns.is_monotonic_increasing)
    # DECISION: merge_asof needs the time key sorted; sort once here, not inside every join
    quotes = quotes.sort_values("ts_ns", kind="stable").reset_index(drop=True)
    assert (quotes.bid < quotes.ask).all(), "crossed quote"            # CHECK: invariant
    assert ticks.ts_ns.is_monotonic_increasing
    series["rank"] = series.groupby("underlying").cumcount()            # position in series_id order
    fills = fills.merge(series[["series_id", "underlying", "delta"]], on="series_id")
    fills = fills.sort_values("ts_ns").reset_index(drop=True)
    fills["sign"] = np.where(fills.side == "B", 1, -1)                  # +1: we bought
    print("STEP 1 data quality:", dq)
    print(f"  ticks={len(ticks):,} quotes={len(quotes):,} fills={len(fills):,} "
          f"contracts={fills.qty.sum():,}")
    return ticks, quotes, fills, series


def our_mid_at(fills: pd.DataFrame, quotes: pd.DataFrame, t_ns: pd.Series) -> np.ndarray:
    """Our own quote mid on the fill's stream at time t (as-of join). Our mid = our fair value."""
    q = quotes[["ts_ns", *STREAM]].assign(mid=(quotes.bid + quotes.ask) / 2)
    left = fills[["fill_id", *STREAM]].assign(ts_ns=t_ns.to_numpy())    # still sorted by time
    out = pd.merge_asof(left, q, on="ts_ns", by=STREAM, direction="backward")
    return out.mid.to_numpy()


def step2_markouts(fills: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    # DECISION: markout_h = sign * (our fair at t+h - fill price), in $ per contract.
    # TRADE-OFF: marking to our own quote mid is model-free here, but inherits our model's errors;
    # marking to the exchange NBBO mid would be independent but noisier for wide option spreads.
    for h in HORIZONS_US:
        fills[f"mo_{h}"] = fills.sign * (our_mid_at(fills, quotes, fills.ts_ns + h * 1000) - fills.price) * MULT
    cols = [f"mo_{h}" for h in HORIZONS_US]
    per_contract = fills[cols].mul(fills.qty, axis=0).sum() / fills.qty.sum()
    print("STEP 2 avg markout $/contract by horizon (us):",
          dict(zip(HORIZONS_US, per_contract.round(2))))
    print(f"  P&L marked at 1s: ${(fills.mo_1000000 * fills.qty).sum():,.0f} for the hour")
    return fills


def step3_classify_stale(fills: pd.DataFrame, ticks: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    t = ticks[["ts_ns", "underlying"]].rename(columns={"ts_ns": "tick_ts"})
    q = quotes[["ts_ns", *STREAM]].rename(columns={"ts_ns": "quote_ts"})
    f = pd.merge_asof(fills, t, left_on="ts_ns", right_on="tick_ts", by="underlying")
    f = pd.merge_asof(f, q, left_on="ts_ns", right_on="quote_ts", by=STREAM)
    # DECISION: a fill is STALE if the underlying moved after our live quote was set and before the
    # fill, i.e. we were hit before our requote landed. Uses only timestamps, no pricing model.
    f["stale"] = f.tick_ts > f.quote_ts
    f["taker_us"] = (f.ts_ns - f.tick_ts) / 1e3
    f["pnl_1s"] = f.mo_1000000 * f.qty
    s = f.groupby("stale").agg(fills=("fill_id", "size"), contracts=("qty", "sum"), pnl_1s=("pnl_1s", "sum"))
    print("STEP 3 fresh vs stale:\n", s.assign(pnl_per_contract=s.pnl_1s / s.contracts).round(2))
    print("  stale fills by venue:", f[f.stale].groupby("venue").size().to_dict(),
          f"| taker reaction p50={f.loc[f.stale, 'taker_us'].median():.1f}us")
    truth = pd.read_parquet(DATA / "_truth.parquet")                    # CHECK: self-check only
    agree = (f.merge(truth, on="fill_id").pipe(lambda x: x.stale == x.is_pickoff)).mean()
    print(f"  CHECK vs truth: classification agrees on {agree:.2%} of fills")
    return f


def step4_requote_latency(quotes: pd.DataFrame, ticks: pd.DataFrame, series: pd.DataFrame,
                          fills: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    q = quotes.sort_values([*STREAM, "ts_ns"])
    prev = q.groupby(STREAM)[["bid", "ask"]].shift()
    mid = (q.bid + q.ask) / 2
    # exposure: how far our NEW fair moved through the OLD quote -> what a taker could grab per contract
    q = q.assign(through=np.maximum(mid - prev.ask, prev.bid - mid))
    q = q.merge(series[["series_id", "underlying", "delta", "rank"]], on="series_id").sort_values("ts_ns")
    t = ticks[["ts_ns", "underlying"]].rename(columns={"ts_ns": "tick_ts"})
    q = pd.merge_asof(q, t, left_on="ts_ns", right_on="tick_ts", by="underlying").dropna(subset=["tick_ts"])
    q["lat_us"] = (q.ts_ns - q.tick_ts) / 1e3                          # tick -> our requote is live
    print("STEP 4 requote latency (us) by venue:\n",
          q.groupby("venue").lat_us.quantile([0.5, 0.99]).unstack().round(1))
    slope = {v: np.polyfit(g["rank"], g.lat_us, 1)[0] for v, g in q.groupby("venue")}
    print("  latency grows with series rank in the batch, us per series:",
          {v: round(float(s), 2) for v, s in slope.items()})

    exposed = q[q.through > 0].copy()
    hit = fills[fills.stale].groupby([*STREAM, "tick_ts"]).size().rename("n_hit").reset_index()
    exposed = exposed.merge(hit, on=[*STREAM, "tick_ts"], how="left")
    exposed["picked"] = exposed.n_hit.fillna(0) > 0
    exposed["lat_bin"] = (exposed.lat_us // LAT_BIN_US) * LAT_BIN_US
    curve = exposed.groupby("lat_bin").picked.mean()                   # the "race curve"
    print(f"  exposures={len(exposed):,}, picked={exposed.picked.mean():.2%}; "
          f"P(picked) by latency bin: {curve.round(3).to_dict()}")
    plot_latency(q, curve)
    return exposed.assign(slope=exposed.venue.map(slope)), curve


def expected_loss(exposed: pd.DataFrame, lat_us: pd.Series, curve: pd.Series) -> float:
    p = np.interp(lat_us, curve.index + LAT_BIN_US / 2, curve.to_numpy())
    return float((p * exposed.through * exposed["size"] * MULT).sum())


def step5_whats_it_worth(exposed: pd.DataFrame, curve: pd.Series, fills: pd.DataFrame) -> None:
    realized = (exposed.picked * exposed.through * exposed["size"] * MULT).sum()
    base = expected_loss(exposed, exposed.lat_us, curve)
    print(f"STEP 5 pick-off loss: realized ${realized:,.0f}/h, curve model ${base:,.0f}/h  (CHECK: close)")
    faster = expected_loss(exposed, (exposed.lat_us - 2).clip(lower=0), curve)
    print(f"  (a) whole path 2us faster: saves ${base - faster:,.0f}/h")
    # (b) send requotes in order of exposure (|delta| * size) instead of series_id order.
    # DECISION: latencies per batch are fixed slots; giving the biggest exposures the earliest slots
    # minimises sum(w_i * P(L_i)) (rearrangement inequality) -> sorting by weight is optimal.
    w = exposed.groupby(["underlying", "venue", "series_id"]).agg(
        w=("delta", lambda d: abs(d.iloc[0])), size=("size", "first"), rank=("rank", "first"))
    w["new_rank"] = (w.w * w["size"]).groupby(level=[0, 1]).rank(ascending=False, method="first") - 1
    shift = (w.new_rank - w["rank"]).rename("d_rank").reset_index()
    e = exposed.merge(shift[["series_id", "venue", "d_rank"]], on=STREAM)
    reordered = expected_loss(e, (e.lat_us + e.slope * e.d_rank).clip(lower=0), curve)
    print(f"  (b) reprioritise requotes by |delta|*size: saves ${base - reordered:,.0f}/h, no hardware needed")
    plot_markouts(fills)


def plot_markouts(f: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7, 4))
    for stale, g in f.groupby("stale"):
        y = [(g[f"mo_{h}"] * g.qty).sum() / g.qty.sum() for h in HORIZONS_US]
        ax.plot(HORIZONS_US, y, marker="o", label="stale (picked off)" if stale else "fresh")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set(xscale="log", xlabel="horizon after fill (us)", ylabel="avg markout ($/contract)",
           title="Markouts: fresh fills earn the spread, stale fills lose")
    ax.legend()
    fig.tight_layout(); fig.savefig(PLOTS / "f6_markouts.png", dpi=110); plt.close(fig)


def plot_latency(q: pd.DataFrame, curve: pd.Series) -> None:
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    q.groupby(["rank", "venue"]).lat_us.median().unstack().plot(ax=ax[0], marker=".")
    ax[0].set(xlabel="series rank in series_id order", ylabel="median requote latency (us)",
              title="Requotes go out one series at a time")
    ax[1].bar(curve.index + LAT_BIN_US / 2, curve.to_numpy(), width=LAT_BIN_US * 0.9)
    ax[1].set(xlabel="our requote latency (us)", ylabel="P(stale quote picked off)",
              title="Race curve vs fast takers")
    fig.tight_layout(); fig.savefig(PLOTS / "f6_requote_latency.png", dpi=110); plt.close(fig)


def main() -> None:
    ticks, quotes, fills, series = step1_load_and_audit()
    fills = step2_markouts(fills, quotes)
    fills = step3_classify_stale(fills, ticks, quotes)
    exposed, curve = step4_requote_latency(quotes, ticks, series, fills)
    step5_whats_it_worth(exposed, curve, fills)


if __name__ == "__main__":
    main()
