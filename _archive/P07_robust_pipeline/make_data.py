"""Generate data for P07 — a small "data lake" of drop-copy fills, partitioned by date and port.

Writes to ../data/p07/lake/:
  date=YYYY-MM-DD/port=N/fills.parquet   one file per (trading date, order-entry port)
  _manifest.json                          what the upstream team says it delivered
  corrections.jsonl                       trade busts / price corrections sent after the fact
"""
import hashlib
import io
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parent.parent / "data" / "p07"
LAKE = OUT / "lake"
SEED = 707

DATES = pd.bdate_range("2026-11-02", periods=5)
PORTS = [0, 1, 2, 3]
SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
           "NFLX", "AVGO", "JPM", "XOM", "SPY", "QQQ", "IWM"]
BASE_PX = dict(zip(SYMBOLS, [231.0, 418.0, 182.0, 205.0, 171.0, 585.0, 312.0, 142.0,
                             780.0, 172.0, 238.0, 118.0, 598.0, 512.0, 228.0]))
VENUES = ["XNAS", "ARCX", "BATS", "XNYS", "EDGX"]


def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf, compression="snappy")
    return buf.getvalue()


def make_partition(rng, date, port, drift):
    n = int(rng.integers(22_000, 34_000))
    open_utc = pd.Timestamp(date, tz="UTC") + pd.Timedelta(hours=14, minutes=30)
    secs = np.sort(rng.uniform(0, 6.5 * 3600, n))
    ts = open_utc + pd.to_timedelta(secs, unit="s")
    w = 1 / np.arange(1, len(SYMBOLS) + 1) ** 0.8
    sym = rng.choice(SYMBOLS, n, p=w / w.sum())
    base = np.array([BASE_PX[s] * drift[s] for s in sym])
    price = np.round(base * (1 + rng.normal(0, 0.002, n)), 2)
    qty = np.clip(np.round(rng.lognormal(4.0, 1.0, n)), 1, 5000).astype("int64")
    df = pd.DataFrame({
        "trade_id": [f"T{date:%Y%m%d}-{port}-{i:06d}" for i in range(n)],
        "ts": ts,
        "symbol": sym,
        "side": rng.choice(["B", "S"], n),
        "price": price,
        "qty": qty,
        "venue": rng.choice(VENUES, n),
        "port": np.int64(port),
    })
    # a few rows that violate basic data-quality rules
    bad = rng.choice(n, 12, replace=False)
    df.loc[bad[:4], "qty"] = 0
    df.loc[bad[4:8], "price"] = -1.0
    df.loc[bad[8:], "side"] = "X"
    return df


def to_v2_format(df):
    """Port 3 was upgraded to the 'v2' drop-copy format from 2026-11-03."""
    v2 = df.rename(columns={"qty": "quantity"})
    v2["price"] = v2["price"].map(lambda x: f"{x:.2f}")
    v2["ts"] = v2["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return v2


def main():
    rng = np.random.default_rng(SEED)
    if LAKE.exists():
        shutil.rmtree(LAKE)
    LAKE.mkdir(parents=True)
    for sub in ("cache", "results", "golden"):
        if (OUT / sub).exists():
            shutil.rmtree(OUT / sub)       # stale caches from an older dataset would be wrong

    manifest, all_ids, price_of, erroneous = [], [], {}, {}
    for date in DATES:
        drift = {s: float(np.exp(rng.normal(0, 0.01))) for s in SYMBOLS}
        parts = {p: make_partition(rng, date, p, drift) for p in PORTS}
        if date <= DATES[1]:
            # "erroneous prints": a few large fills booked ~10% away from the market, corrected later
            p0 = parts[0]
            clean = np.flatnonzero((p0.price > 0) & (p0.qty > 0) & p0.side.isin(["B", "S"]))
            for i in rng.choice(clean, 3, replace=False):
                tid = p0.at[i, "trade_id"]
                erroneous[tid] = float(p0.at[i, "price"])
                p0.at[i, "price"] = round(erroneous[tid] * 0.9, 2)
                p0.at[i, "qty"] = int(rng.integers(4000, 5001))
        # failover: some of port 1's fills were re-sent on port 2 (same trade_id)
        resend = parts[1].sample(frac=0.004, random_state=int(rng.integers(1 << 30))).assign(port=np.int64(2))
        parts[2] = pd.concat([parts[2], resend]).sort_values("ts", kind="stable").reset_index(drop=True)

        for port, df in parts.items():
            rel = f"date={date:%Y-%m-%d}/port={port}/fills.parquet"
            out = df if not (port == 3 and date >= pd.Timestamp("2026-11-03")) else to_v2_format(df)
            raw = parquet_bytes(out)
            manifest.append({"path": rel, "rows": len(out), "sha256": hashlib.sha256(raw).hexdigest()})
            own = df[df.port == port]
            all_ids.extend(own.trade_id.tolist())
            price_of.update(zip(own.trade_id, own.price))
            path = LAKE / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if rel.startswith("date=2026-11-04/port=2"):
                continue                                        # never delivered
            if rel.startswith("date=2026-11-05/port=1"):
                path.write_bytes(raw[: int(len(raw) * 0.6)])    # truncated upload -> corrupt
                continue
            if rel.startswith("date=2026-11-06/port=0"):
                path.write_bytes(parquet_bytes(out.iloc[: int(len(out) * 0.8)]))  # partial delivery
                continue
            path.write_bytes(raw)

    # a test session's file that is not in the manifest
    stray = make_partition(rng, DATES[-1], 9, {s: 1.0 for s in SYMBOLS}).head(500)
    (LAKE / "date=2026-11-06/port=9").mkdir(parents=True, exist_ok=True)
    (LAKE / "date=2026-11-06/port=9/fills.parquet").write_bytes(parquet_bytes(stray))
    # the missing partition's directory should not exist either
    (LAKE / "date=2026-11-04/port=2").rmdir()

    (LAKE / "_manifest.json").write_text(json.dumps(
        {"producer": "dropcopy-exporter", "generated_at": "2026-11-07T01:00:00Z", "files": manifest}, indent=1))

    # corrections: busts and price corrections, applied in seq order
    ids = np.setdiff1d(np.array(all_ids), list(erroneous))
    busts = rng.choice(ids, 150, replace=False)
    corr = rng.choice(np.setdiff1d(ids, busts), 100, replace=False)
    events = [{"action": "BUST", "trade_id": t} for t in busts]
    events += [{"action": "CORRECT", "trade_id": t,
                "new_price": round(price_of[t] * (1 + float(rng.choice([-1, 1])) * 0.003), 2)} for t in corr]
    events += [{"action": "CORRECT", "trade_id": t, "new_price": px} for t, px in erroneous.items()]
    events += [{"action": "BUST", "trade_id": t} for t in corr[:2]]          # corrected, later busted
    events += [{"action": "BUST", "trade_id": f"T20261104-7-{i:06d}"} for i in range(3)]  # unknown ids
    events.append(dict(events[0]))                                           # duplicate message
    # keep "corrected, later busted" in that order; shuffle everything else
    k = 150 + 100 + len(erroneous)
    tail = events[k:k + 2]
    rest = [events[i] for i in rng.permutation(len(events)) if i not in (k, k + 1)]
    rest.extend(tail)
    with open(LAKE / "corrections.jsonl", "w") as f:
        for seq, e in enumerate(rest, start=1):
            f.write(json.dumps({"seq": seq, **e}) + "\n")

    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"{p.relative_to(OUT.parent.parent)}  {p.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
