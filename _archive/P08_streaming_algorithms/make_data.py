#!/usr/bin/env python3
"""Generate market-data captures from 4 ports + an order-send log for P08.

Writes:
  data/p08/port_{0..3}.jsonl   one JSON object per line, in ARRIVAL order per port
  data/p08/orders.jsonl        our order sends (all sessions), sorted by ts_ns
"""
import json
import string
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 808
OUT = Path(__file__).resolve().parent.parent / "data" / "p08"
START_NS = int(pd.Timestamp("2026-11-12 14:30:00", tz="UTC").value)
DURATION_S = 300
N_PORTS = 4
BASE_RATE = 310            # msgs / s / port
JITTER_NS = 2_000_000      # normal delivery disorder bound (2 ms)

rng = np.random.default_rng(SEED)


def make_symbols(n=50):
    syms = set()
    while len(syms) < n:
        k = int(rng.integers(2, 5))
        syms.add("".join(rng.choice(list(string.ascii_uppercase), size=k)))
    return sorted(syms)


SYMBOLS = make_symbols()
_w = 1.0 / np.arange(1, len(SYMBOLS) + 1) ** 1.1
SYM_P = _w / _w.sum()
HOT_SYMBOL = SYMBOLS[47]           # normally rare; becomes busy near the end


def latency_us(ts_s):
    n = len(ts_s)
    lat = rng.lognormal(np.log(25), 0.5, n)
    tail = rng.random(n) < 0.005
    lat[tail] = np.minimum(200 * (1 + rng.pareto(1.5, tail.sum())), 20_000)
    degraded = (ts_s >= 180) & (ts_s < 210)        # a 30 s congestion episode
    lat[degraded] *= 4
    return np.maximum(1, np.round(lat)).astype(int)


def market_data():
    # base Poisson flow per port
    per_port = []
    for p in range(N_PORTS):
        n = rng.poisson(BASE_RATE * DURATION_S)
        ts = np.sort(rng.uniform(0, DURATION_S, n))
        sym = rng.choice(len(SYMBOLS), size=n, p=SYM_P)
        per_port.append([ts, sym])

    # hot symbol in the last 60 s (35 msgs/s/port extra)
    for p in range(N_PORTS):
        n = rng.poisson(35 * 60)
        per_port[p][0] = np.concatenate([per_port[p][0], rng.uniform(240, DURATION_S, n)])
        per_port[p][1] = np.concatenate([per_port[p][1], np.full(n, SYMBOLS.index(HOT_SYMBOL))])

    # microbursts: all ports at once, sub-millisecond
    starts = np.sort(rng.choice(np.arange(10, 290, 5.0), size=12, replace=False) + rng.uniform(0, 4, 12))
    for s in starts:
        size = int(rng.integers(150, 600))
        span = rng.uniform(0.0003, 0.0009)
        ts = s + rng.uniform(0, span, size)
        port = rng.integers(0, N_PORTS, size)
        sym = rng.choice(len(SYMBOLS), size=size, p=SYM_P)
        for p in range(N_PORTS):
            m = port == p
            per_port[p][0] = np.concatenate([per_port[p][0], ts[m]])
            per_port[p][1] = np.concatenate([per_port[p][1], sym[m]])

    for p, (ts_s, sym) in enumerate(per_port):
        order = np.argsort(ts_s, kind="stable")
        ts_s, sym = ts_s[order], sym[order]
        ts_ns = START_NS + (ts_s * 1e9).astype(np.int64)
        n = len(ts_ns)
        jitter = rng.integers(0, JITTER_NS, n)
        very_late = rng.choice(np.arange(1000, n - 1000), size=5, replace=False)
        jitter[very_late] = rng.integers(20_000_000, 80_000_000, 5)
        arrival = ts_ns + jitter
        deliver = np.argsort(arrival, kind="stable")
        lat = latency_us(ts_s)
        types = rng.choice(["quote", "trade", "status"], size=n, p=[0.85, 0.12, 0.03])
        lines = []
        for seq, i in enumerate(deliver, 1):
            lines.append(json.dumps({"ts_ns": int(ts_ns[i]), "port": p, "seq": seq, "sym": SYMBOLS[sym[i]],
                                     "type": str(types[i]), "lat_us": int(lat[i])}, separators=(",", ":")))
        (OUT / f"port_{p}.jsonl").write_text("\n".join(lines) + "\n")


def max_window_count(ts_sorted, window_ns=1_000_000_000):
    best, left = 0, 0
    for right in range(len(ts_sorted)):
        while ts_sorted[right] - ts_sorted[left] >= window_ns:
            left += 1
        best = max(best, right - left + 1)
    return best


def orders():
    sessions = {}
    for name, rate in (("S1", 4), ("S2", 22), ("S3", 12)):
        n = rng.poisson(rate * DURATION_S)
        sessions[name] = list(np.sort(rng.uniform(0, DURATION_S, n)))
    sessions["S2"] += list(95.0 + rng.uniform(0, 0.200, 45))        # burst
    sessions["S3"] += list(200.0 + rng.uniform(0, 0.050, 70))       # burst

    # S1: a burst that lands EXACTLY on the limit (50 in some window) -> boundary case
    burst = list(150.0 + np.sort(rng.uniform(0, 0.100, 60)))
    base = sessions["S1"]
    for m in range(30, 61):
        cand = np.sort(START_NS + (np.array(base + burst[:m]) * 1e9).astype(np.int64))
        if max_window_count(cand) == 50:
            sessions["S1"] = base + burst[:m]
            break

    rows = []
    for name, times in sessions.items():
        ts = np.sort(START_NS + (np.array(times) * 1e9).astype(np.int64))
        if name == "S2":                         # a few identical timestamps
            for i in rng.choice(np.arange(10, len(ts) - 10), size=8, replace=False):
                ts[i] = ts[i - 1]
        for i, t in enumerate(ts):
            rows.append((int(t), name, f"{name}-{i:06d}"))
    rows.sort(key=lambda r: r[0])
    lines = [json.dumps({"ts_ns": t, "session": s, "order_id": o}, separators=(",", ":")) for t, s, o in rows]
    (OUT / "orders.jsonl").write_text("\n".join(lines) + "\n")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    market_data()
    orders()
    for p in sorted(OUT.iterdir()):
        n = sum(1 for _ in open(p))
        print(f"{p.relative_to(OUT.parent.parent)}  {n:>8,} lines  {p.stat().st_size / 1e6:5.1f} MB")


if __name__ == "__main__":
    main()
