"""P07 reference solution — a robust, cached, concurrent ingestion pipeline for drop-copy fills.

Run:  python3 solution.py            (after python3 make_data.py)
      python3 solution.py --bless    (also (re)write the golden regression file)

Parts: A inventory/validation -> B fault-tolerant + concurrent fetch -> C normalize/validate ->
       D persistent cache -> E dedupe, corrections, daily summary, report -> (F tests in test_solution.py)
"""
import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flaky_store as store  # noqa: E402  (provided infrastructure)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p07"
LAKE = DATA / "lake"
CACHE = DATA / "cache"
RESULTS = DATA / "results"
GOLDEN = DATA / "golden" / "daily_summary.csv"
PLOTS = ROOT / "plots"

SCHEMA_VERSION = 1            # bump whenever normalization logic changes -> old cache entries are ignored
CANONICAL = ["trade_id", "ts", "symbol", "side", "price", "qty", "venue", "port"]
RENAMES = {"quantity": "qty"}  # v2 drop-copy format
PART_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})/port=(\d+)/fills\.parquet$")
RETRYABLE = (store.TransientStoreError, TimeoutError)   # asyncio.TimeoutError is TimeoutError since 3.11


class SchemaError(Exception):
    pass


@dataclass
class Partition:
    path: str
    date: str
    port: int
    expected_rows: int | None = None
    expected_sha: str | None = None
    status: str = "PENDING"   # OK | INCOMPLETE | CHECKSUM_MISMATCH | CORRUPT | SCHEMA_ERROR | MISSING | FETCH_FAILED | UNEXPECTED
    rows: int = 0
    attempts: int = 0
    from_cache: bool = False
    detail: str = ""


# ----------------------------------------------------------------------------- context managers
@contextmanager
def timed(label, sink):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        sink[label] = time.perf_counter() - t0


@contextmanager
def atomic_path(final):
    """Yield a temp path next to `final`; rename it into place only if the block succeeds.

    os.replace() is atomic on POSIX (same filesystem), so a crash mid-write never leaves a
    half-written cache/result file that a later run would trust."""
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.tmp-{os.getpid()}")
    try:
        yield tmp
        os.replace(tmp, final)
    finally:
        if tmp.exists():
            tmp.unlink()


# ----------------------------------------------------------------------------- Part A: inventory
def discover_partitions(lake):
    """Generator over the store listing: yields (rel_path, date, port). Lazy, so it also works
    for a listing with millions of keys (paginate in real life)."""
    for rel in store.list_objects(lake):
        m = PART_RE.search(rel)
        if m:
            yield rel, m.group(1), int(m.group(2))


def inventory(lake, manifest):
    expected = {f["path"]: f for f in manifest["files"]}
    found = {rel: (d, p) for rel, d, p in discover_partitions(lake)}
    parts = {}
    for rel, meta in expected.items():
        d, p = PART_RE.search(rel).groups()
        part = Partition(rel, d, int(p), meta["rows"], meta["sha256"])
        if rel not in found:
            part.status, part.detail = "MISSING", "listed in manifest but not in store"
        parts[rel] = part
    for rel, (d, p) in found.items():
        if rel not in expected:
            parts[rel] = Partition(rel, d, p, status="UNEXPECTED", detail="not in manifest -> ignored")
    return parts


# ----------------------------------------------------------------------------- Part B: fetch with retries
def backoff_delay(attempt, base=0.05, cap=1.0):
    """Exponential backoff with full jitter: random in [0, min(cap, base*2^attempt)].
    Jitter stops many clients from retrying in lock-step and hammering a recovering server."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


def fetch_sync(lake, rel, max_attempts=6):
    for attempt in range(1, max_attempts + 1):
        try:
            return store.fetch_bytes_sync(lake, rel), attempt
        except RETRYABLE:
            if attempt == max_attempts:
                raise
            time.sleep(backoff_delay(attempt))
    # NotFoundError is NOT caught: retrying a permanent error only wastes time


async def fetch_async(lake, rel, sem, max_attempts=6, timeout=0.3):
    for attempt in range(1, max_attempts + 1):
        try:
            async with sem:                       # cap concurrency below the store's throttle limit
                data = await asyncio.wait_for(store.fetch_bytes(lake, rel), timeout)  # client-side timeout
            return data, attempt
        except RETRYABLE:
            if attempt == max_attempts:
                raise
            await asyncio.sleep(backoff_delay(attempt))   # back off OUTSIDE the semaphore: don't hold a slot


def fetch_all_sequential(lake, rels):
    return sum(fetch_sync(lake, r)[1] for r in rels)


async def fetch_all_async(lake, rels, concurrency):
    sem = asyncio.Semaphore(concurrency)
    res = await asyncio.gather(*(fetch_async(lake, r, sem) for r in rels), return_exceptions=True)
    return sum(r[1] for r in res if not isinstance(r, BaseException)), sum(isinstance(r, BaseException) for r in res)


# ----------------------------------------------------------------------------- Part C: normalize/validate
def normalize_schema(df):
    """Map any known producer format onto the canonical schema and dtypes."""
    df = df.rename(columns=RENAMES)
    missing = set(CANONICAL) - set(df.columns)
    if missing:
        raise SchemaError(f"missing columns {sorted(missing)}")
    ts = df["ts"]
    ts = ts.dt.tz_convert("UTC") if isinstance(ts.dtype, pd.DatetimeTZDtype) else pd.to_datetime(ts, utc=True, format="ISO8601")
    return pd.DataFrame({
        "trade_id": df["trade_id"].astype(str),
        "ts": ts.astype("datetime64[ns, UTC]"),
        "symbol": df["symbol"].astype(str).str.strip().str.upper(),
        "side": df["side"].astype(str).str.strip().str.upper(),
        "price": pd.to_numeric(df["price"], errors="coerce").astype("float64"),
        "qty": pd.to_numeric(df["qty"], errors="coerce").astype("Int64"),   # nullable until validated
        "venue": df["venue"].astype(str),
        "port": df["port"].astype("int64"),
    })


def validate_rows(df):
    """Split into (good, quarantined). Quarantine instead of dropping silently: someone must review."""
    reason = np.select(
        [df.price.isna() | (df.price <= 0), df.qty.isna() | (df.qty <= 0), ~df.side.isin(["B", "S"])],
        ["bad_price", "bad_qty", "bad_side"], default="")
    bad = reason != ""
    good = df[~bad].assign(qty=lambda x: x.qty.astype("int64"))
    return good, df[bad].assign(reason=reason[bad])


def price_outliers(trades, tol=0.05):
    """Plausibility check: fills more than `tol` away from that day's median price for the symbol.
    Kept (they may be real) but surfaced -- erroneous prints are usually busted/corrected later."""
    med = trades.groupby(["date", "symbol"]).price.transform("median")
    return trades[(trades.price / med - 1).abs() > tol]


def parse_partition(raw, part):
    """bytes -> (normalized DataFrame | None, status, detail). Pure function: easy to unit test."""
    sha = hashlib.sha256(raw).hexdigest()
    try:
        df = pq.read_table(io.BytesIO(raw)).to_pandas()
    except (pa.ArrowInvalid, OSError) as e:          # truncated / not parquet: permanent, don't retry
        return None, "CORRUPT", f"unreadable parquet: {str(e)[:60]}"
    try:
        df = normalize_schema(df)
    except SchemaError as e:
        return None, "SCHEMA_ERROR", str(e)
    if part.expected_rows is not None and len(df) < part.expected_rows:
        return df, "INCOMPLETE", f"{len(df)}/{part.expected_rows} rows delivered"
    if part.expected_sha and sha != part.expected_sha:
        return df, "CHECKSUM_MISMATCH", "content differs from manifest"
    return df, "OK", ""


# ----------------------------------------------------------------------------- Part D: persistent cache
def cache_paths(cache_dir, rel, etag):
    """Cache key = (object path, object ETag, SCHEMA_VERSION). If upstream re-delivers the file the
    ETag changes; if our parsing code changes we bump SCHEMA_VERSION. Either one -> cache miss."""
    stem = rel.replace("/fills.parquet", "").replace("/", "__").replace("=", "-")
    base = Path(cache_dir) / f"{stem}.{etag[:12]}.v{SCHEMA_VERSION}"
    return base.with_suffix(".parquet"), base.with_suffix(".json")


async def load_partition(lake, part, cache_dir, sem, use_cache=True):
    etag = store.stat_object(lake, part.path)["etag"]          # cheap HEAD request, no download
    data_path, marker = cache_paths(cache_dir, part.path, etag)
    if use_cache and marker.exists():                           # marker is written LAST = commit flag
        info = json.loads(marker.read_text())
        part.status, part.detail, part.from_cache = info["status"], info["detail"], True
        df = pd.read_parquet(data_path)
        part.rows = len(df)
        return df.assign(date=part.date)
    try:
        raw, part.attempts = await fetch_async(lake, part.path, sem)
    except store.NotFoundError:
        part.status, part.detail = "MISSING", "store returned 404"
        return None
    except RETRYABLE as e:
        part.status, part.detail = "FETCH_FAILED", f"gave up after retries: {e!r}"
        return None
    # parquet decoding happens in C++ and releases the GIL, so a worker thread really runs in parallel
    df, part.status, part.detail = await asyncio.to_thread(parse_partition, raw, part)
    if df is None:
        return None          # failures are NOT cached: a re-delivery or a code fix should be picked up
    part.rows = len(df)
    with atomic_path(data_path) as tmp:
        df.to_parquet(tmp, index=False)
    with atomic_path(marker) as tmp:
        tmp.write_text(json.dumps({"status": part.status, "detail": part.detail, "rows": part.rows}))
    return df.assign(date=part.date)


async def run_pipeline_async(lake, manifest, cache_dir, concurrency=8, use_cache=True):
    parts = inventory(lake, manifest)
    sem = asyncio.Semaphore(concurrency)
    todo = [p for p in parts.values() if p.status == "PENDING"]
    frames = await asyncio.gather(*(load_partition(lake, p, cache_dir, sem, use_cache) for p in todo))
    frames = [f for f in frames if f is not None]
    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=CANONICAL + ["date"])
    return parts, raw


# ----------------------------------------------------------------------------- Part E: business logic
def dedupe(df):
    """trade_id is the business key. After a session failover the same fill is re-sent on another
    port; keep the copy from the port that owns the trade (encoded in the id)."""
    owner = df.trade_id.str.split("-").str[1].astype("int64")
    ranked = df.assign(_own=(df.port == owner)).sort_values(["trade_id", "_own"], ascending=[True, False])
    dup = ranked.duplicated("trade_id", keep="first")
    return ranked[~dup].drop(columns="_own").sort_values("ts").reset_index(drop=True), int(dup.sum())


def apply_corrections(trades, corrections, parts):
    """Apply busts/price corrections in seq order; the LAST action per trade wins.
    Idempotent: re-applying the same file gives the same result."""
    c = corrections.sort_values("seq").drop_duplicates(["trade_id", "action", "new_price"])
    last = c.groupby("trade_id").tail(1)
    known = last.trade_id.isin(trades.trade_id)
    busts = set(last.trade_id[known & (last.action == "BUST")])
    fixes = last[known & (last.action == "CORRECT")].set_index("trade_id").new_price
    out = trades[~trades.trade_id.isin(busts)].copy()
    hit = out.trade_id.isin(fixes.index)
    out.loc[hit, "price"] = out.loc[hit, "trade_id"].map(fixes)

    # classify what we could not apply, so nothing is silently lost
    def why(tid):
        _, port, _ = tid.split("-")
        date = f"{tid[1:5]}-{tid[5:7]}-{tid[7:9]}"
        part = parts.get(f"date={date}/port={port}/fills.parquet")
        if part is None:
            return "UNKNOWN_PARTITION"
        if part.status in ("OK", "CHECKSUM_MISMATCH"):
            return "UNKNOWN_TRADE"
        return f"PENDING ({part.status})"          # retry once the partition is re-delivered
    unmatched = last[~known].assign(reason=lambda x: x.trade_id.map(why))
    stats = {"busts_applied": len(busts), "corrections_applied": int(hit.sum()),
             "duplicate_msgs": len(corrections) - len(c), "unmatched": len(unmatched)}
    return out, unmatched, stats


def daily_summary(trades, parts):
    notional = trades.price * trades.qty
    g = trades.assign(notional=notional).groupby(["date", "symbol"])
    s = g.agg(n_trades=("trade_id", "size"), volume=("qty", "sum"), notional=("notional", "sum")).reset_index()
    s["vwap"] = (s.notional / s.volume).round(4)
    # A day is only complete if every manifest port for that date loaded cleanly.
    ok = pd.Series({p.date: True for p in parts.values() if p.status != "UNEXPECTED"})
    for p in parts.values():
        if p.status not in ("OK", "UNEXPECTED"):
            ok[p.date] = False
    s["complete"] = s.date.map(ok).fillna(False).astype(bool)
    return s.drop(columns="notional")


def frame_fingerprint(df):
    return hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()[:16]


def report(parts, raw, quarantined, n_dups, corr_stats, unmatched, before, after):
    print("\n== Part A/C: partition report")
    rep = pd.DataFrame([asdict(p) for p in parts.values()])[
        ["path", "status", "rows", "expected_rows", "attempts", "from_cache", "detail"]].sort_values("path")
    print(rep.to_string(index=False))
    print(f"\nrows loaded={len(raw):,}  quarantined={len(quarantined)} "
          f"{quarantined.reason.value_counts().to_dict()}  cross-port duplicates removed={n_dups}")
    print(f"corrections: {corr_stats}")
    print("unmatched corrections by reason:", unmatched.reason.value_counts().to_dict())

    print("\n== Part E: impact of corrections on daily VWAP (top 5, in bps)")
    m = before.merge(after, on=["date", "symbol"], suffixes=("_before", "_after"))
    m["vwap_bps"] = (m.vwap_after / m.vwap_before - 1) * 1e4
    m["volume_diff"] = m.volume_after - m.volume_before
    print(m.reindex(m.vwap_bps.abs().sort_values(ascending=False).index)
           [["date", "symbol", "vwap_before", "vwap_after", "vwap_bps", "volume_diff", "complete_after"]]
           .head(5).round(3).to_string(index=False))
    incomplete = sorted(after.loc[~after.complete, "date"].unique())
    print(f"dates whose totals are INCOMPLETE (must be labelled as such downstream): {incomplete}")
    return rep


def plot_partitions(parts, timings):
    rows = [p for p in parts.values() if p.status != "UNEXPECTED"]
    dates = sorted({p.date for p in rows})
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.5), gridspec_kw={"width_ratios": [2.2, 1]})
    w = 0.2
    for p in rows:
        x = dates.index(p.date) + (p.port - 1.5) * w
        color = {"OK": "C0", "INCOMPLETE": "C1"}.get(p.status, "C3")
        ax[0].bar(x, p.rows, w * 0.9, color=color)
        if p.status != "OK":
            ax[0].text(x, max(p.rows, 1500), p.status, rotation=90, ha="center", va="bottom", fontsize=8)
    ax[0].set_xticks(range(len(dates)), dates)
    ax[0].set(title="rows loaded per partition (bars = ports 0..3)", ylabel="rows")
    labels = list(timings)
    ax[1].barh(labels, [timings[k] for k in labels], color="C2")
    ax[1].set(title="wall time (s)", xscale="log")
    fig.tight_layout()
    fig.savefig(PLOTS / "p07_pipeline_report.png", dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bless", action="store_true", help="write the golden regression file")
    args = ap.parse_args()
    random.seed(0)
    store.configure(seed=11)
    manifest = json.loads((LAKE / "_manifest.json").read_text())
    fetchable = [p.path for p in inventory(LAKE, manifest).values() if p.status == "PENDING"]
    timings = {}

    print("== Part B: fetching", len(fetchable), "partitions through the flaky store")
    with timed("sequential fetch", timings):
        attempts = fetch_all_sequential(LAKE, fetchable)
    print(f"sequential: {timings['sequential fetch']:.2f}s, {attempts} attempts, store stats {store.STATS}")
    store.configure()
    with timed("async fetch (8 in flight)", timings):
        attempts, failed = asyncio.run(fetch_all_async(LAKE, fetchable, concurrency=8))
    print(f"async x8:   {timings['async fetch (8 in flight)']:.2f}s, {attempts} attempts, failed={failed}, store stats {store.STATS}")
    store.configure()
    asyncio.run(fetch_all_async(LAKE, fetchable, concurrency=20))
    print(f"async x20:  store throttled {store.STATS['throttled']} requests (429) -> more concurrency is not free")

    shutil.rmtree(CACHE, ignore_errors=True)
    store.configure()
    with timed("pipeline, cold cache", timings):
        parts, raw = asyncio.run(run_pipeline_async(LAKE, manifest, CACHE))
    store.configure()
    with timed("pipeline, warm cache", timings):
        warm_parts, warm_raw = asyncio.run(run_pipeline_async(LAKE, manifest, CACHE))
    hits = sum(p.from_cache for p in warm_parts.values())
    refetched = [p.path for p in warm_parts.values() if p.attempts > 0]
    print(f"\n== Part D: cold pipeline {timings['pipeline, cold cache']:.2f}s vs warm {timings['pipeline, warm cache']:.2f}s")
    print(f"warm run: {hits} partitions served from cache, store downloads={store.STATS['requests']} "
          f"(re-fetched: {refetched} -- failures are deliberately not cached)")
    assert frame_fingerprint(warm_raw) == frame_fingerprint(raw), "cache must not change results"

    good, quarantined = validate_rows(raw)
    trades, n_dups = dedupe(good)
    corrections = pd.read_json(LAKE / "corrections.jsonl", lines=True)
    before = daily_summary(trades, parts)
    fixed, unmatched, corr_stats = apply_corrections(trades, corrections, parts)
    after = daily_summary(fixed, parts)
    out_before, out_after = price_outliers(trades), price_outliers(fixed)
    print(f"\nprice outliers (>5% from date-symbol median): before corrections={len(out_before)}, after={len(out_after)}")
    print(out_before[["trade_id", "symbol", "price", "qty"]].to_string(index=False))
    rep = report(parts, raw, quarantined, n_dups, corr_stats, unmatched, before, after)

    # persistent results: atomic writes, deterministic content -> idempotent re-runs
    with atomic_path(RESULTS / "daily_summary.parquet") as tmp:
        after.to_parquet(tmp, index=False)
    with atomic_path(RESULTS / "partition_report.csv") as tmp:
        rep.to_csv(tmp, index=False)
    with atomic_path(RESULTS / "quarantine.csv") as tmp:
        quarantined.to_csv(tmp, index=False)
    fp = frame_fingerprint(after)
    print(f"\nresults written to {RESULTS} (summary fingerprint {fp})")

    if args.bless:
        with atomic_path(GOLDEN) as tmp:
            after.to_csv(tmp, index=False)
        print(f"golden file written: {GOLDEN}")
    elif GOLDEN.exists():
        same = frame_fingerprint(pd.read_csv(GOLDEN)) == frame_fingerprint(pd.read_csv(io.StringIO(after.to_csv(index=False))))
        print(f"regression vs golden: {'PASS' if same else 'FAIL -> inspect the diff before blessing'}")
    plot_partitions(parts, timings)
    print(f"plot: {PLOTS / 'p07_pipeline_report.png'}")


if __name__ == "__main__":
    main()
