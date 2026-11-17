"""Generate data for P10 — per-minute server monitoring metrics + on-call incident labels.

Writes to ../data/p10/:
  metrics.parquet            per-minute metrics for 6 servers x 15 trading days
  incidents_labeled.csv      incidents labeled by on-call engineers, days 1-10 (use for tuning)
  incidents_holdout.csv      incidents for days 11-15 (open ONLY for the final evaluation)
"""
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data" / "p10"
SEED = 1010

SERVERS = [f"srv-{i}" for i in range(1, 7)]
SHARE = dict(zip(SERVERS, [1.0, 0.9, 1.1, 1.0, 0.8, 1.2]))
DAYS = pd.bdate_range("2026-10-26", periods=15)          # 15 trading days
MINUTES = np.arange(390)                                  # 09:30 .. 15:59 ET

# Acute incidents: (server, day_index, start_minute, duration_min, p99_multiplier)
SPIKES = [
    ("srv-1", 0, 142, 6, 3.5), ("srv-3", 1, 4, 5, 3.0), ("srv-5", 2, 251, 9, 4.0),
    ("srv-2", 3, 61, 4, 2.6), ("srv-6", 4, 330, 7, 3.2), ("srv-4", 6, 8, 6, 3.4),
    ("srv-1", 7, 200, 12, 2.8), ("srv-3", 8, 377, 5, 4.5), ("srv-5", 9, 95, 3, 5.0),
    ("srv-6", 10, 2, 8, 3.0), ("srv-2", 11, 170, 6, 3.6), ("srv-4", 12, 290, 4, 2.7),
    ("srv-1", 13, 45, 10, 3.1), ("srv-3", 14, 360, 6, 4.0),
]
# Packet-drop bursts: (server, day_index, start_minute, duration_min)
DROPS = [("srv-6", 1, 33, 3), ("srv-2", 5, 312, 2), ("srv-4", 9, 150, 4),
         ("srv-5", 12, 20, 2), ("srv-3", 14, 101, 3)]


def main():
    rng = np.random.default_rng(SEED)
    OUT.mkdir(parents=True, exist_ok=True)

    intraday = 1 + 2.0 * np.exp(-MINUTES / 25) + 1.2 * np.exp(-(389 - MINUTES) / 20)  # U shape
    day_factor = rng.lognormal(0, 0.15, len(DAYS))
    day_factor[9] *= 1.35                                       # one busy day

    frames = []
    for d, day in enumerate(DAYS):
        # market-wide component shared by all servers (news, volatility)
        market = intraday * day_factor[d] * rng.lognormal(0, 0.05, len(MINUTES))
        if d == 5:                                              # 10:00 ET economic release
            market[30:37] *= 3.0
        for srv in SERVERS:
            rate = 40 * SHARE[srv] * market * rng.lognormal(0, 0.06, len(MINUTES))  # k msgs/s
            load = rate / 40
            p50 = (3.0 + 1.6 * load) * rng.lognormal(0, 0.04, len(MINUTES))
            # heavy-tailed noise in log space -> some benign outliers
            tail_noise = 0.07 * rng.standard_t(4, len(MINUTES))
            p99 = (10 + 8 * load ** 1.5) * np.exp(tail_noise)
            lam = 0.02 * load ** 3
            drops = rng.poisson(lam)
            cpu = np.clip(15 + 14 * load + rng.normal(0, 2, len(MINUTES)), 0, 100)

            # chronic: deploy regression on srv-4 from day index 8
            if srv == "srv-4" and d >= 8:
                p50 *= 1.15
                p99 *= 1.25
                cpu += 3
            # chronic: memory leak on srv-2, days index 11 and 12 (restarted overnight)
            if srv == "srv-2" and d in (11, 12):
                p99 *= 1 + 0.004 * MINUTES
                p50 *= 1 + 0.001 * MINUTES

            for s_srv, s_d, s_m, dur, mult in SPIKES:
                if s_srv == srv and s_d == d:
                    sl = slice(s_m, s_m + dur)
                    p99[sl] *= mult * rng.uniform(0.85, 1.15, dur)
                    p50[sl] *= 1.3
                    cpu[sl] = np.clip(cpu[sl] + 10, 0, 100)
            for s_srv, s_d, s_m, dur in DROPS:
                if s_srv == srv and s_d == d:
                    drops[s_m:s_m + dur] = rng.integers(60, 500, dur)
                    p99[s_m:s_m + dur] *= 1.4

            ts = day + pd.Timedelta(hours=9, minutes=30) + pd.to_timedelta(MINUTES, unit="min")
            frames.append(pd.DataFrame({
                "ts": ts, "server": srv,
                "msg_rate_k": np.round(rate, 2),
                "p50_us": np.round(p50, 2), "p99_us": np.round(p99, 2),
                "drops": drops.astype("int64"), "cpu_pct": np.round(cpu, 1),
            }))

    df = pd.concat(frames, ignore_index=True)

    # --- data-quality problems ---------------------------------------------
    # monitoring agent on srv-5 was down for 20 minutes on day index 2
    gap = (df.server == "srv-5") & (df.ts.dt.normalize() == DAYS[2]) & \
          (df.ts >= DAYS[2] + pd.Timedelta("11:30:00")) & (df.ts < DAYS[2] + pd.Timedelta("11:50:00"))
    df = df[~gap]
    # agent restart on srv-6 re-sent 30 minutes of rows (slightly different cpu sample)
    dup = df[(df.server == "srv-6") & (df.ts >= DAYS[6] + pd.Timedelta("12:50:00")) &
             (df.ts < DAYS[6] + pd.Timedelta("13:20:00"))].copy()
    dup["cpu_pct"] = np.round(dup["cpu_pct"] + rng.normal(0, 0.5, len(dup)), 1)
    df = pd.concat([df, dup], ignore_index=True)
    # a few missing p99 samples
    nan_idx = rng.choice(len(df), 8, replace=False)
    df.loc[df.index[nan_idx], "p99_us"] = np.nan
    # rows arrive grouped by server in the warehouse, not globally sorted
    df = df.sort_values(["server", "ts"], kind="stable").reset_index(drop=True)
    df.to_parquet(OUT / "metrics.parquet", index=False)

    # --- labels ----------------------------------------------------------------
    rows = []
    for srv, d, m, dur, _ in SPIKES:
        rows.append((srv, d, m, dur))
    for srv, d, m, dur in DROPS:
        rows.append((srv, d, m, dur))
    lab = pd.DataFrame(rows, columns=["server", "d", "m", "dur"])
    lab["start_ts"] = [DAYS[d] + pd.Timedelta(hours=9, minutes=30 + m) for d, m in zip(lab.d, lab.m)]
    lab["end_ts"] = lab.start_ts + pd.to_timedelta(lab.dur - 1, unit="min")
    lab = lab.sort_values("start_ts").reset_index(drop=True)
    lab["incident_id"] = [f"INC-{1000 + i}" for i in range(len(lab))]
    lab["note"] = "on-call ticket"
    cols = ["incident_id", "server", "start_ts", "end_ts", "note"]
    lab[lab.d < 10][cols].to_csv(OUT / "incidents_labeled.csv", index=False)
    lab[lab.d >= 10][cols].to_csv(OUT / "incidents_holdout.csv", index=False)

    for f in sorted(OUT.iterdir()):
        print(f"{f.relative_to(OUT.parent.parent)}  {f.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
