#!/usr/bin/env python3
"""Generate data/p02/: ticks.csv (~3M rows, a few malformed), ticks_small.csv + expected answers.

Deterministic (fixed seed). Run:  python3 make_data.py
"""
import json
import string
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 2027
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "p02"
N_ROWS = 3_000_000
N_SYMBOLS = 500
HEADER = "timestamp,symbol,price,size"

FIXED = {  # a few recognisable names; the rest are random tickers
    "AAPL": 231.40, "MSFT": 418.20, "NVDA": 147.90, "SPY": 591.00, "TSLA": 321.00,
    "AMZN": 205.10, "QQQ": 512.40, "META": 583.20, "AMD": 139.50, "GOOGL": 176.30,
    "AZO": 3105.00, "BKNG": 4912.30, "NVR": 7820.00, "MKL": 1520.00,   # > $1,000
}


def make_symbols(rng) -> tuple[list[str], np.ndarray]:
    syms = list(FIXED)
    seen = set(syms)
    letters = np.array(list(string.ascii_uppercase))
    while len(syms) < N_SYMBOLS:
        s = "".join(rng.choice(letters, rng.integers(2, 5)))
        if s not in seen:
            seen.add(s)
            syms.append(s)
    base = np.array([FIXED.get(s, 0.0) for s in syms])
    rand = base == 0
    base[rand] = np.round(np.exp(rng.uniform(np.log(5), np.log(600), rand.sum())), 2)
    return syms, base


def small_file() -> tuple[str, dict]:
    rows = [
        HEADER,
        "2026-11-12T14:30:00.000001,AAPL,100.00,100",
        "2026-11-12T14:30:00.000002,AAPL,101.50,200",
        "2026-11-12T14:30:00.000003, aapl ,102.25,100",      # normalise -> AAPL
        "2026-11-12T14:30:00.000004,MSFT,400.10,50",
        "2026-11-12T14:30:00.000005,MSFT,,10",                # empty price
        "2026-11-12T14:30:00.000006,MSFT,N/A,10",             # not a number
        "2026-11-12T14:30:00.000007,MSFT,-400.10,10",         # negative
        "",                                                   # blank line
        HEADER,                                               # header repeated (files concatenated)
        '2026-11-12T14:30:00.000008,BKNG,"4,912.30",5',       # thousands separator -> 4912.30
        "2026-11-12T14:30:00.000009,BKNG,4900.70,5",
        "2026-11-12T14:30:00.000010,TSLA,321.00,100,EXTRA",   # extra column
        "2026-11-12T14:30:00.000011,TSLA",                    # too few columns
        "2026-11-12T14:30:00.000012,TSLA,320.5,100",          # one decimal
        "2026-11-12T14:30:00.000013,TSLA,322,100",            # no decimals
        "2026-11-12T14:30:00.000014,ZERO,0.00,100",           # zero price
        "2026-11-12T14:30:00.000015,SPY,590.10,100",
        "2026-11-12T14:30:00.000016,SPY,590.20,100",
        "2026-11-12T14:30:00.000017,SPY,590.30,100",
        "2026-11-12T14:30:00.000018,,590.30,100",             # empty symbol
        "2026-11-12T14:30:00.000019,QQQ,abc,100",             # garbage price
        "2026-11-12T14:30:00.000020,QQQ,512.40,100",
    ]
    expected = {
        "policy": "skip rows with wrong field count, empty symbol, missing/non-numeric/non-positive "
                  "price; skip blank lines and repeated headers; strip+upper-case symbols; accept "
                  "thousands separators in quoted prices",
        "averages": {"AAPL": 101.25, "MSFT": 400.10, "BKNG": 4906.50, "TSLA": 321.25,
                     "SPY": 590.20, "QQQ": 512.40},
        "sum_cents": {"AAPL": 30375, "MSFT": 40010, "BKNG": 981300, "TSLA": 64250,
                      "SPY": 177060, "QQQ": 51240},
        "counts": {"AAPL": 3, "MSFT": 1, "BKNG": 2, "TSLA": 2, "SPY": 3, "QQQ": 1},
        "accepted_rows": 12, "rejected_rows": 8, "blank_lines": 1, "repeated_headers": 1,
    }
    return "\n".join(rows) + "\n", expected


def main() -> None:
    rng = np.random.default_rng(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    syms, base = make_symbols(rng)

    # --- symbol per row: Zipf-like popularity -------------------------------
    w = 1.0 / np.arange(1, N_SYMBOLS + 1) ** 1.1
    sym_idx = rng.choice(N_SYMBOLS, N_ROWS, p=w / w.sum())

    # --- timestamps: one trading day, sorted, microsecond resolution --------
    start = np.datetime64("2026-11-12T14:30:00", "us")
    offs = np.sort(rng.integers(0, int(6.5 * 3600 * 1e6), N_ROWS))
    ts = np.datetime_as_string(start + offs.astype("timedelta64[us]"), unit="us")

    # --- prices: per-symbol random walk (so per-minute averages move) -------
    order = np.argsort(sym_idx, kind="stable")          # rows grouped by symbol, time-ordered
    counts = np.bincount(sym_idx, minlength=N_SYMBOLS)
    step_sigma = 0.02 / np.sqrt(np.maximum(counts, 1))   # ~2% daily vol per symbol
    noise = rng.normal(0, 1, N_ROWS) * step_sigma[sym_idx[order]]
    csum = np.cumsum(noise)
    group_start = np.repeat(np.concatenate([[0], np.cumsum(counts)[:-1]]), counts)
    walk = np.empty(N_ROWS)
    walk[order] = csum - csum[group_start] + noise[group_start]
    cents = np.round(base[sym_idx] * np.exp(walk) * 100).astype(np.int64)
    sizes = rng.choice([1, 10, 50, 100, 100, 100, 200, 300, 500, 1000], N_ROWS)

    sym_col = np.array(syms, dtype=object)[sym_idx]
    price_col = pd.Series(cents // 100).astype(str) + "." + pd.Series(cents % 100).astype(str).str.zfill(2)
    price_col = price_col.to_numpy(dtype=object)
    accepted = np.ones(N_ROWS, dtype=bool)

    # --- in-field corruption ------------------------------------------------
    def pick(k, mask=None):
        pool = np.flatnonzero(accepted if mask is None else (accepted & mask))
        return rng.choice(pool, k, replace=False)

    for i in pick(300):
        price_col[i] = ""; accepted[i] = False
    for i in pick(300):
        price_col[i] = "N/A"; accepted[i] = False
    for i in pick(200):
        price_col[i] = "-" + price_col[i]; accepted[i] = False
    for i in pick(50):
        price_col[i] = "0.00"; accepted[i] = False
    for i in pick(30):
        sym_col[i] = ""; accepted[i] = False
    for i in pick(200, cents >= 100_000):                  # >= $1,000 -> "1,234.50" (accepted)
        c = int(cents[i]); price_col[i] = f"{c // 100:,}.{c % 100:02d}"
    for i in pick(300):                                     # " aapl", "msft ", "Nvda" (accepted)
        s = sym_col[i]
        sym_col[i] = rng.choice([f" {s.lower()}", f"{s} ", s.capitalize(), f" {s} "])

    df = pd.DataFrame({"timestamp": ts, "symbol": sym_col, "price": price_col, "size": sizes})

    # --- whole-line corruption, inserted between chunks ---------------------
    extra = []
    for _ in range(100):
        j = int(rng.integers(N_ROWS))
        extra.append(f"{ts[j]},{syms[sym_idx[j]]},{cents[j] // 100}.{cents[j] % 100:02d},{sizes[j]},X")
    for _ in range(30):
        j = int(rng.integers(N_ROWS))
        extra.append(f"{ts[j]},{syms[sym_idx[j]]}")
    extra += [""] * 20 + [HEADER] * 3
    rng.shuffle(extra)

    path = OUT_DIR / "ticks.csv"
    n_chunks = 300
    bounds = np.linspace(0, N_ROWS, n_chunks + 1).astype(int)
    slots = set(rng.choice(np.arange(1, n_chunks), len(extra), replace=False).tolist())
    extra_iter = iter(extra)
    with open(path, "w", newline="") as f:
        for k in range(n_chunks):
            if k in slots:
                f.write(next(extra_iter) + "\n")
            df.iloc[bounds[k]:bounds[k + 1]].to_csv(f, header=(k == 0), index=False, lineterminator="\n")

    # --- independent oracle for the big file (integer cents => exact) -------
    norm_sym = pd.Series(sym_col).str.strip().str.upper().to_numpy()
    acc = pd.DataFrame({"sym": norm_sym[accepted], "cents": cents[accepted]})
    g = acc.groupby("sym")["cents"].agg(["sum", "count"])
    expected_big = {
        "sum_cents": {k: int(v) for k, v in g["sum"].items()},
        "counts": {k: int(v) for k, v in g["count"].items()},
        "accepted_rows": int(accepted.sum()),
    }
    (OUT_DIR / "expected_ticks.json").write_text(json.dumps(expected_big))

    small, expected_small = small_file()
    (OUT_DIR / "ticks_small.csv").write_text(small)
    (OUT_DIR / "expected_small.json").write_text(json.dumps(expected_small, indent=2))

    for name in ["ticks.csv", "expected_ticks.json", "ticks_small.csv", "expected_small.json"]:
        p = OUT_DIR / name
        print(f"wrote {p}  ({p.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
