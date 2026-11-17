#!/usr/bin/env python3
"""Generate data/p01/gateway.log -- a messy order-gateway log (15 minutes, 4 gateways).

Deterministic (fixed seed). Run:  python3 make_data.py
"""
from pathlib import Path

import numpy as np

SEED = 20261112
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "p01"

DAY = "2026-11-12"
NS = 1_000_000_000
US = 1_000
MS = 1_000_000
T0 = (14 * 3600 + 25 * 60) * NS          # 14:25:00 UTC (ns since midnight)
DURATION = 900 * NS                      # 15 minutes
OPEN = (14 * 3600 + 30 * 60) * NS        # 14:30:00 UTC = US cash open
OPEN_LEN = 30 * NS

GATEWAYS = ["gw-1", "gw-2", "gw-3", "gw-4"]
BASE_RATE = 22.0                         # orders / s / gateway
OPEN_EXTRA_RATE = 88.0                   # extra orders / s during the open burst (5x total)

VENUES = np.array(["XNAS", "ARCX", "BATS"])
VENUE_P = [0.45, 0.25, 0.30]
RTT_MEDIAN_US = {"XNAS": 35.0, "ARCX": 55.0, "BATS": 30.0}

SYMBOLS = {
    "AAPL": 231.40, "MSFT": 418.20, "NVDA": 147.90, "AMZN": 205.10, "GOOGL": 176.30,
    "META": 583.20, "TSLA": 321.00, "AMD": 139.50, "NFLX": 821.60, "JPM": 241.80,
    "XOM": 120.30, "BAC": 45.20, "SPY": 591.00, "QQQ": 512.40, "IWM": 236.70,
}
SYM_NAMES = np.array(list(SYMBOLS))

STALL_PERIOD = 10 * NS                   # gw-3 periodic hiccup
STALL_WINDOW = 120 * MS
HICCUP_START = (14 * 3600 + 36 * 60 + 41) * NS   # ARCX stops answering for ~20 s
HICCUP_LEN = 20 * NS

TRACEBACKS = [
    [
        "Traceback (most recent call last):",
        '  File "/opt/gw/handler.py", line 212, in on_message',
        "    self._route(msg)",
        '  File "/opt/gw/router.py", line 88, in _route',
        "    raise KeyError(venue)",
        "KeyError: 'XNYS'",
    ],
    [
        "Traceback (most recent call last):",
        '  File "/opt/gw/handler.py", line 230, in on_message',
        "    payload = self.codec.encode(msg)",
        '  File "/opt/gw/codec.py", line 51, in encode',
        "    return struct.pack(self.fmt, *fields)",
        "struct.error: argument out of range",
    ],
]


def fmt_ts(t_ns: int, nanos: bool) -> str:
    sec, frac = divmod(int(t_ns), NS)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if nanos:
        return f"{DAY} {h:02d}:{m:02d}:{s:02d}.{frac:09d}"
    return f"{DAY} {h:02d}:{m:02d}:{s:02d}.{frac // US:06d}"


def arrival_times(rng) -> np.ndarray:
    n_base = rng.poisson(BASE_RATE * DURATION / NS)
    n_open = rng.poisson(OPEN_EXTRA_RATE * OPEN_LEN / NS)
    t = np.concatenate([
        T0 + rng.integers(0, DURATION, n_base),
        OPEN + rng.integers(0, OPEN_LEN, n_open),
    ])
    t.sort()
    return t


def lognormal_ns(rng, median_us: float, sigma: float) -> int:
    return int(rng.lognormal(np.log(median_us * US), sigma))


def gen_gateway(rng, g_idx: int, gw: str, entries: list) -> None:
    nanos = gw == "gw-4"                  # newer build logs nanoseconds
    tag = f"G{g_idx + 1}"
    times = arrival_times(rng)
    new_orders = []                       # (clid, sym, venue) for cancels to reference
    counter = 0

    def add(t, level, thread, body):
        entries.append((int(t), len(entries), f"{fmt_ts(t, nanos)} {gw} {level:<5} [{thread}] {body}"))

    for t_in in times:
        counter += 1
        clid = f"{tag}-{counter:07d}"
        thread = f"t{rng.integers(2, 5)}"
        is_cancel = bool(new_orders) and rng.random() < 0.25
        if is_cancel:
            orig, sym, venue = new_orders[rng.integers(max(0, len(new_orders) - 200), len(new_orders))]
            add(t_in, "INFO", thread, f"IN  Cancel clid={clid} orig={orig} sym={sym}")
            internal = lognormal_ns(rng, 5.0, 0.35)
        else:
            sym = SYM_NAMES[rng.integers(len(SYM_NAMES))]
            venue = VENUES[rng.choice(3, p=VENUE_P)]
            side = "BUY" if rng.random() < 0.5 else "SELL"
            px = round(SYMBOLS[sym] * (1 + rng.normal(0, 0.002)), 2)
            qty = int(rng.choice([100, 100, 100, 200, 300, 500, 1000]))
            add(t_in, "INFO", thread,
                f"IN  NewOrder clid={clid} sym={sym} side={side} px={px:.2f} qty={qty}")
            new_orders.append((clid, sym, venue))
            internal = lognormal_ns(rng, 8.0, 0.35)

        # gw-3: a periodic background job steals the core for ~120 ms every 10 s
        if gw == "gw-3" and (t_in % STALL_PERIOD) < STALL_WINDOW:
            internal += int(rng.uniform(1.0, 4.0) * MS)

        # rare internal failure: ERROR + traceback, order never leaves the gateway
        if rng.random() < 0.0005:
            t_err = t_in + int(rng.uniform(3, 15) * US)
            tb = TRACEBACKS[rng.integers(len(TRACEBACKS))]
            text = f"{fmt_ts(t_err, nanos)} {gw} ERROR [{thread}] handler exception while processing clid={clid}"
            entries.append((t_err, len(entries), "\n".join([text] + tb)))
            continue

        t_out = t_in + internal
        add(t_out, "INFO", thread, f"OUT {'Cancel' if is_cancel else 'NewOrder'} clid={clid} venue={venue}")

        # venue response
        in_open = OPEN <= t_out < OPEN + OPEN_LEN
        in_hiccup = HICCUP_START <= t_out < HICCUP_START + HICCUP_LEN
        if (venue == "ARCX" and in_hiccup and rng.random() < 0.6) or rng.random() < 0.0002:
            continue                      # no response ever arrives
        median, sigma = RTT_MEDIAN_US[venue], 0.30
        if in_open:
            median, sigma = median * 1.5, 0.60
        if is_cancel:
            median *= 0.9

        if (not is_cancel) and gw == "gw-2" and in_open and rng.random() < 0.15:
            reason = "RATE_LIMIT"
        elif rng.random() < 0.008:
            reason = (rng.choice(["TOO_LATE_TO_CANCEL", "UNKNOWN_ORDER"]) if is_cancel
                      else rng.choice(["PRICE_BAND", "INVALID_QTY", "PRICE_BAND"]))
        else:
            reason = None
        if reason:
            t_rsp = t_out + lognormal_ns(rng, median * 0.8, sigma)
            add(t_rsp, "WARN", "t1", f"REJ clid={clid} reason={reason}")
        else:
            t_rsp = t_out + lognormal_ns(rng, median, sigma)
            add(t_rsp, "INFO", "t1", f"ACK clid={clid} exch_id={int(rng.integers(10**6, 10**8))}")

    # heartbeats (1 s) and session stats (30 s)
    offset = int(rng.integers(0, 900)) * MS
    for k, t in enumerate(range(T0 + offset, T0 + DURATION, NS)):
        add(t, "DEBUG", "t4", f"HB seq={k + 1}")
    sent = 0
    for t in range(T0 + offset + 7 * MS, T0 + DURATION, 30 * NS):
        sent += int(rng.integers(500, 900))
        add(t, "INFO", "t4",
            f"session stats: sent={sent} acked={int(sent * 0.985)} rejected={int(sent * 0.01)} "
            f"open={int(rng.integers(20, 80))}")


def main() -> None:
    rng = np.random.default_rng(SEED)
    entries: list = []                    # (t_ns, tiebreak, text)
    for g_idx, gw in enumerate(GATEWAYS):
        gen_gateway(rng, g_idx, gw, entries)
    entries.sort()
    lines = [e[2] for e in entries]

    # --- messiness -------------------------------------------------------
    n = len(lines)
    single = lambda i: "\n" not in lines[i]

    # (1) truncated lines (log rotation cut a write in half)
    for i in rng.choice(n, 14, replace=False):
        if single(i):
            lines[i] = lines[i][: int(rng.integers(18, max(19, len(lines[i]) - 6)))]

    # (2) adjacent lines written out of order by different threads (~0.5%)
    for i in rng.choice(n - 1, int(n * 0.005), replace=False):
        lines[i], lines[i + 1] = lines[i + 1], lines[i]

    # (3) exact duplicate lines (logger retried a write, ~0.2%)
    dup_idx = set(rng.choice(n, int(n * 0.002), replace=False).tolist())
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if i in dup_idx and single(i):
            out.append(line)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "gateway.log"
    text = "\n".join(out) + "\n"
    path.write_text(text)
    print(f"wrote {path}  ({text.count(chr(10)):,} lines, {path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
