"""Generate the four small datasets for the plotting drills into ./data (fixed seed)."""
import numpy as np
import pandas as pd

from style import DATA

rng = np.random.default_rng(2026)
DATA.mkdir(exist_ok=True)
OPEN = pd.Timestamp("2026-11-12 09:30")
DAYS = pd.bdate_range("2026-10-15", periods=20)


def d1_latency(n=200_000):
    """Order-gateway latency for one day: knee under load, gw-3 slow mode, gw-2 afternoon tail drift."""
    sec = np.sort(rng.uniform(0, 390 * 60, n))
    m = sec / 60
    load = 25 + 55 * np.exp(-m / 20) + 35 * np.exp(-(390 - m) / 15) + rng.gamma(2, 4, n)
    lat = rng.lognormal(np.log(9), 0.35, n) * (1 + np.clip(load - 60, 0, None) / 30) ** 2
    gw = rng.choice(["gw-1", "gw-2", "gw-3", "gw-4"], n)
    slow3 = (gw == "gw-3") & (rng.random(n) < 0.05)
    lat[slow3] += rng.uniform(150, 300, slow3.sum())
    leak2 = (gw == "gw-2") & (rng.random(n) < 0.04)
    lat[leak2] *= 1 + 12 * (m[leak2] / 390) ** 2
    pd.DataFrame({"ts": OPEN + pd.to_timedelta(sec, unit="s"), "gateway": gw,
                  "latency_us": lat.round(2), "load_msgs_per_ms": load.round(1)}
                 ).to_parquet(DATA / "d1_latency.parquet", index=False)


def d2_market():
    """One symbol: 1-second quotes for one day (10:00 news shock) + 20 days of per-minute stats."""
    s = np.arange(390 * 60)
    mid = 100 + np.cumsum(rng.normal(0, 0.006, len(s)))
    mid[1800:] -= np.clip((s[1800:] - 1800) / 20, 0, 1) * 1.5            # news at 10:00:00
    spread = rng.choice([1, 2, 3], len(s), p=[0.7, 0.25, 0.05])
    spread[:600] += rng.integers(0, 3, 600)                              # wider after the open
    spread[1800:1840] = rng.integers(5, 11, 40)                         # blown out on the news
    bid = np.round(mid - spread * 0.005, 2)
    vol_rate = 40 * (1 + 3 * np.exp(-s / 900) + 2 * np.exp(-(s.max() - s) / 900))
    vol_rate[1800:1860] *= 15
    pd.DataFrame({"ts": OPEN + pd.to_timedelta(s, unit="s"), "bid": bid,
                  "ask": np.round(bid + spread * 0.01, 2), "volume": rng.poisson(vol_rate) * 10}
                 ).to_parquet(DATA / "d2_quotes_1s.parquet", index=False)

    rows = []
    for d, day in enumerate(DAYS):
        m = np.arange(390)
        vol = 2e4 * (1 + 3 * np.exp(-m / 20) + 2.5 * np.exp(-(389 - m) / 15)) * rng.lognormal(0, 0.12) \
            * rng.lognormal(0, 0.25, 390)
        spr = (3 + 7 * np.exp(-m / 12)) * rng.lognormal(0, 0.15, 390)
        if d == 13:
            spr[30:45] *= 4                                              # a news day at 10:00
        rows.append(pd.DataFrame({"date": day, "minute": m, "volume": vol.round(), "spread_bps": spr.round(2)}))
    pd.concat(rows).to_parquet(DATA / "d2_intraday_20d.parquet", index=False)


def d3_orders(n=60_000):
    """Our routed orders over 20 days: fill rate by venue, slippage vs size, routing-mix shift."""
    day_idx = rng.integers(0, len(DAYS), n)
    t = day_idx / (len(DAYS) - 1)
    p = np.stack([0.40 - 0.20 * t, np.full(n, 0.25), 0.15 + 0.20 * t, np.full(n, 0.1975), np.full(n, 0.0025)], 1)
    venue = np.array(["A", "B", "C", "D", "E"])[(rng.random(n)[:, None] > np.cumsum(p, 1)).sum(1)]
    minute = rng.integers(0, 390, n)
    fill_p = pd.Series(venue).map({"A": 0.92, "B": 0.88, "C": 0.90, "D": 0.86, "E": 0.79}).to_numpy()
    fill_p = np.where((venue == "D") & (minute < 30), 0.62, fill_p)
    filled = rng.random(n) < fill_p
    size = np.clip(rng.lognormal(np.log(500), 1.0, n), 100, 20_000).round(-2)
    slip = 0.5 + 0.8 * np.sqrt(size / 1000) + rng.standard_t(3, n) * 0.6
    pd.DataFrame({"date": DAYS[day_idx], "ts": DAYS[day_idx] + pd.Timedelta("09:30:00") + pd.to_timedelta(minute, "min"),
                  "venue": venue, "order_size": size.astype(int), "filled": filled,
                  "slippage_bps": np.where(filled, slip.round(2), np.nan)}
                 ).to_parquet(DATA / "d3_orders.parquet", index=False)


def d4_pnl(n_days=120):
    """Daily PnL of four strategies + S1 PnL by symbol. S4 got a new version on day 80."""
    dates = pd.bdate_range("2026-05-01", periods=n_days)
    z1, z2 = rng.normal(size=(2, n_days))
    s1 = 20_000 + 30_000 * z1
    s2 = 15_000 + 35_000 * (0.8 * z1 + 0.6 * z2)
    s3 = 12_000 + 45_000 * rng.standard_t(3, n_days)
    s3[40:66] -= 30_000
    s3[52] = -420_000
    s4 = np.where(np.arange(n_days) < 80, 12_000, -3_000) + 25_000 * rng.normal(size=n_days)
    pd.DataFrame({"date": np.tile(dates, 4), "strategy": np.repeat(["S1", "S2", "S3", "S4"], n_days),
                  "pnl": np.concatenate([s1, s2, s3, s4]).round()}).to_parquet(DATA / "d4_pnl_daily.parquet", index=False)
    w = rng.pareto(1.2, 40) + 0.05
    w[rng.choice(40, 6, replace=False)] = -np.array([0.35, 0.2, 0.1, 0.05, 0.04, 0.03]) * w.max()   # some symbols lose
    sym = [f"SYM{i:02d}" for i in range(40)]
    pd.DataFrame({"symbol": sym, "pnl": (w / w.sum() * s1.sum()).round()}).to_parquet(DATA / "d4_pnl_by_symbol.parquet", index=False)


if __name__ == "__main__":
    d1_latency(); d2_market(); d3_orders(); d4_pnl()
    for f in sorted(DATA.iterdir()):
        print(f"{f.name:26s} {f.stat().st_size / 1e6:5.2f} MB")
