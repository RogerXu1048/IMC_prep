"""Generate synthetic A/B multicast feed captures for P04.

Writes ../data/p04/feed_A.parquet and ../data/p04/feed_B.parquet.
Deterministic (fixed seed). Do not read this file before attempting the problem:
the generative model is effectively the answer key.
"""
from pathlib import Path
import time

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data" / "p04"
SEED = 20261110

WINDOW_S = 2 * 3600                      # each capture covers 14:30:00-16:30:00 UTC
MSG_TYPES = np.array(["ADD", "CANCEL", "MODIFY", "TRADE", "IMBALANCE"])
TYPE_P = np.array([0.46, 0.38, 0.10, 0.05, 0.01])
TYPE_PAYLOAD = np.array([46, 30, 42, 52, 64])          # bytes of payload per type
HEADER_BYTES = 20                                      # sequenced-packet header
WIRE_OVERHEAD = 42 + 20                                # eth/ip/udp headers + preamble/IFG
NS_PER_BYTE_1G = 8.0                                   # 1 Gb/s cross-connect

DAYS = {
    # date, target messages, burst-size median, intra-burst gap median (ns)
    "2026-11-10": dict(n=300_000, size_median=18, gap_median_ns=1_800),
    "2026-11-11": dict(n=480_000, size_median=22, gap_median_ns=1_250),
}

LINES = {
    # (date, line) -> (base latency ns, noise median ns, noise sigma)
    ("2026-11-10", "A"): (18_000, 800, 0.50),
    ("2026-11-11", "A"): (18_000, 800, 0.50),
    ("2026-11-10", "B"): (19_500, 1_000, 0.90),
    ("2026-11-11", "B"): (16_800, 800, 0.50),
}


def exchange_send_times(rng, n_target, size_median, gap_median_ns):
    """Bursty exchange send times (ns since window start), sorted."""
    sigma = 1.0
    mean_size = 1 + size_median * np.exp(sigma**2 / 2)
    n_bursts = int(n_target / mean_size)
    sizes = 1 + np.floor(rng.lognormal(np.log(size_median), sigma, n_bursts)).astype(np.int64)
    sizes = np.minimum(sizes, 2_000)
    starts = rng.uniform(0, WINDOW_S * 1e9, n_bursts)
    burst_gap = rng.lognormal(np.log(gap_median_ns), 0.7, n_bursts)   # each burst has its own pace
    burst_id = np.repeat(np.arange(n_bursts), sizes)
    gaps = rng.exponential(1.0, sizes.sum()) * burst_gap[burst_id]
    # first message of each burst starts at the burst start (gap 0)
    first = np.r_[0, np.cumsum(sizes)[:-1]]
    gaps[first] = 0.0
    # cumulative gap within burst
    csum = np.cumsum(gaps)
    offset_in_burst = csum - np.repeat(csum[first], sizes)
    t = starts[burst_id] + offset_in_burst
    t = np.sort(t)
    t = t[t < WINDOW_S * 1e9]
    # exchange stamps are integer ns, non-decreasing in sequence-number order
    return np.floor(t).astype(np.int64)


def lindley_departures(arrival, service):
    """FIFO single-server queue: D_i = max(a_i, D_{i-1}) + s_i (vectorised)."""
    s_cum = np.cumsum(service)
    s_prev = s_cum - service
    return s_cum + np.maximum.accumulate(arrival - s_prev)


def main():
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    frames = {"A": [], "B": []}

    for date, cfg in DAYS.items():
        day_start = pd.Timestamp(f"{date} 14:30:00", tz="UTC").value
        rel = exchange_send_times(rng, cfg["n"], cfg["size_median"], cfg["gap_median_ns"])
        n = len(rel)
        exch_ns = day_start + rel
        seq = np.arange(1, n + 1, dtype=np.int64)
        tidx = rng.choice(len(MSG_TYPES), size=n, p=TYPE_P)
        msg_type = MSG_TYPES[tidx]
        nbytes = (TYPE_PAYLOAD[tidx] + HEADER_BYTES).astype(np.int16)
        service = (nbytes.astype(np.float64) + WIRE_OVERHEAD) * NS_PER_BYTE_1G

        # receiver clock is ahead of the exchange clock by 3us and drifts +2us over the window
        frac = rel / (WINDOW_S * 1e9)
        clock_offset = 3_000 + 2_000 * frac

        # messages lost upstream (before the A/B split): a few short clusters
        n_clusters = 3 if date.endswith("10") else 4
        lost_both = set()
        for c_start in rng.choice(np.arange(1000, n - 1000), n_clusters, replace=False):
            k = int(rng.integers(3, 7))
            lost_both.update(range(int(c_start), int(c_start) + k))

        for line in ("A", "B"):
            base, noise_med, noise_sigma = LINES[(date, line)]
            noise = rng.lognormal(np.log(noise_med), noise_sigma, n)
            # work in ns relative to the window start: epoch ns (~1.8e18) does not fit
            # exactly in float64 (ulp = 256 ns), so floats must stay small
            arrival = np.maximum.accumulate(rel + noise)              # FIFO through the switch
            depart = lindley_departures(arrival, service)
            wait = depart - service - arrival
            rx = depart + base + clock_offset
            rx_ns = day_start + np.round(rx).astype(np.int64)

            keep = np.ones(n, dtype=bool)
            keep[np.array(sorted(lost_both))] = False
            if line == "A":
                keep &= rng.random(n) > 0.0005                         # iid loss
            else:
                # shallow buffer on B: tail drop when the queue is deep
                deep = wait > 150_000
                keep &= ~(deep & (rng.random(n) < 0.35))
            df = pd.DataFrame({
                "date": date,
                "seq": seq[keep],
                "exch_ns": exch_ns[keep],
                "rx_ns": rx_ns[keep],
                "msg_type": msg_type[keep],
                "bytes": nbytes[keep],
            })
            if line == "A" and date.endswith("11"):
                # a mirror-port glitch duplicates a handful of packets
                dup_idx = rng.choice(len(df), 25, replace=False)
                dups = df.iloc[dup_idx].copy()
                dups["rx_ns"] = dups["rx_ns"] + rng.integers(60, 600, len(dups))
                df = pd.concat([df, dups], ignore_index=True)
            df = df.sort_values("rx_ns", kind="stable").reset_index(drop=True)
            if line == "B" and date.endswith("10"):
                # a few adjacent packets swapped on the wire (rx order != seq order)
                cols = ["seq", "exch_ns", "msg_type", "bytes"]
                close = np.flatnonzero(np.diff(df["rx_ns"].to_numpy()) < 3_000)
                close = close[(close > 10) & (close < len(df) - 10)]
                for i in rng.choice(close, 15, replace=False):
                    a, b = df.loc[i, cols].copy(), df.loc[i + 1, cols].copy()
                    df.loc[i, cols] = b.values
                    df.loc[i + 1, cols] = a.values
            frames[line].append(df)

    for line, parts in frames.items():
        out = pd.concat(parts, ignore_index=True)
        out["date"] = out["date"].astype("category")
        out["msg_type"] = out["msg_type"].astype("category")
        path = OUT / f"feed_{line}.parquet"
        out.to_parquet(path, index=False)
        print(f"wrote {path.relative_to(OUT.parent.parent)}  rows={len(out):,}  "
              f"size={path.stat().st_size / 1e6:.1f} MB")
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
