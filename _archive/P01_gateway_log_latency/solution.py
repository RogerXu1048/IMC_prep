#!/usr/bin/env python3
"""P01 reference solution -- order-gateway log -> processing-latency analysis.

Run:  python3 solution.py          (after python3 make_data.py)
Prints results for Parts A-F and writes figures to data_practice/plots/p01_*.png
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "p01" / "gateway.log"
PLOTS = ROOT / "plots"
PLOTS.mkdir(exist_ok=True)

NS_PER_US = 1_000
NS_PER_S = 1_000_000_000

# ---------------------------------------------------------------------------
# Part A -- streaming parser
# ---------------------------------------------------------------------------
# One anchored regex for the common prefix; the "body" is parsed with str.split
# (cheaper than one giant regex with many optional groups).
PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.(\d{6}|\d{9}) (gw-\d+) ([A-Z]+)\s+\[(t\d+)\] (.*)$"
)
CONTINUATION_RE = re.compile(r"^(\s|Traceback|[\w.]+(Error|Exception)\b)")
REQUIRED_KEYS = {
    ("IN", "NewOrder"): {"clid", "sym", "side", "px", "qty"},
    ("IN", "Cancel"): {"clid", "orig", "sym"},
    ("OUT", "NewOrder"): {"clid", "venue"},
    ("OUT", "Cancel"): {"clid", "venue"},
    ("ACK", None): {"clid", "exch_id"},
    ("REJ", None): {"clid", "reason"},
}
KNOWN_VENUES = {"XNAS", "ARCX", "BATS"}
EVENT_COLS = ["ts_ns", "gw", "level", "thread", "ev", "mtype", "clid",
              "sym", "side", "px", "qty", "orig", "venue", "reason"]


@lru_cache(maxsize=4096)
def epoch_seconds(date: str, hms: str) -> int:
    """Cache per distinct second: 15 min of logs = only 900 distinct values."""
    dt = datetime.strptime(f"{date} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iter_events(path: Path, stats: Counter) -> Iterator[tuple]:
    """Yield one tuple per order event. O(1) memory: a generator over the file.

    stats counts what we skipped so the caller can report data quality.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            stats["lines"] += 1
            line = raw.rstrip("\n")
            if not line.strip():
                stats["blank"] += 1
                continue
            m = PREFIX_RE.match(line)
            if m is None:
                if CONTINUATION_RE.match(line):
                    stats["traceback_continuation"] += 1   # belongs to previous ERROR
                else:
                    stats["malformed"] += 1                # e.g. truncated timestamp
                continue
            date, hms, frac, gw, level, thread, body = m.groups()
            # 6-digit (us) vs 9-digit (ns) fractions: normalise to integer ns.
            frac_ns = int(frac) * NS_PER_US if len(frac) == 6 else int(frac)
            ts_ns = epoch_seconds(date, hms) * NS_PER_S + frac_ns

            tokens = body.split()
            ev = tokens[0]
            if ev in ("HB",):
                stats["heartbeat"] += 1
                continue
            if body.startswith("session stats"):
                stats["session_stats"] += 1
                continue
            if level == "ERROR":
                stats["error"] += 1
                clid = next((t[5:] for t in tokens if t.startswith("clid=")), None)
                yield (ts_ns, gw, level, thread, "ERR", None, clid,
                       None, None, None, None, None, None, None)
                continue
            if ev not in ("IN", "OUT", "ACK", "REJ"):
                stats["malformed"] += 1
                continue
            mtype = None
            kv_tokens = tokens[1:]
            if ev in ("IN", "OUT"):
                if len(tokens) < 2 or tokens[1] not in ("NewOrder", "Cancel"):
                    stats["malformed"] += 1
                    continue
                mtype, kv_tokens = tokens[1], tokens[2:]
            kv = dict(t.split("=", 1) for t in kv_tokens if "=" in t)
            # Validate: a truncated line usually loses its last key(s).
            if not REQUIRED_KEYS[(ev, mtype)] <= kv.keys():
                stats["malformed"] += 1
                continue
            if ev == "OUT" and kv["venue"] not in KNOWN_VENUES:
                stats["malformed"] += 1                    # e.g. "venue=XN" (cut)
                continue
            try:
                px = float(kv["px"]) if "px" in kv else None
                qty = int(kv["qty"]) if "qty" in kv else None
            except ValueError:
                stats["malformed"] += 1
                continue
            stats["events"] += 1
            yield (ts_ns, gw, level, thread, ev, mtype, kv["clid"], kv.get("sym"),
                   kv.get("side"), px, qty, kv.get("orig"), kv.get("venue"), kv.get("reason"))


def part_a_parse(path: Path = LOG_PATH) -> pd.DataFrame:
    stats: Counter = Counter()
    t0 = time.perf_counter()
    events = pd.DataFrame.from_records(iter_events(path, stats), columns=EVENT_COLS)
    elapsed = time.perf_counter() - t0

    # Exact duplicate log lines -> identical (gw, clid, ev, ts). Drop and count.
    dup_mask = events.duplicated(subset=["gw", "clid", "ev", "ts_ns"])
    stats["duplicate_events"] = int(dup_mask.sum())
    events = events.loc[~dup_mask].reset_index(drop=True)
    # Same (clid, ev) but a DIFFERENT timestamp would be a real conflict (resend?)
    conflict = events.duplicated(subset=["gw", "clid", "ev"], keep=False) & events["ev"].ne("ERR")
    stats["conflicting_duplicates"] = int(conflict.sum())

    # File order != time order? (multi-threaded logger)
    out_of_order = int((np.diff(events["ts_ns"].to_numpy()) < 0).sum())

    print("== Part A: parsing ==")
    print(f"parsed {stats['lines']:,} lines in {elapsed:.2f}s "
          f"({stats['lines'] / elapsed / 1e3:.0f}k lines/s)")
    for k in ["events", "heartbeat", "session_stats", "error", "traceback_continuation",
              "malformed", "blank", "duplicate_events", "conflicting_duplicates"]:
        print(f"  {k:<24}{stats[k]:>10,}")
    print(f"  {'lines out of time order':<24}{out_of_order:>10,}  (=> never rely on file order)")
    print(f"  gateways with ns timestamps: "
          f"{sorted(events.loc[events.ts_ns % 1000 != 0, 'gw'].unique())}")
    return events


# ---------------------------------------------------------------------------
# Part B -- one row per order
# ---------------------------------------------------------------------------
def part_b_orders(events: pd.DataFrame) -> pd.DataFrame:
    # Event timestamps -> columns IN / OUT / ACK / REJ / ERR (min handles leftovers)
    times = events.pivot_table(index="clid", columns="ev", values="ts_ns", aggfunc="min")
    for col in ["IN", "OUT", "ACK", "REJ", "ERR"]:
        if col not in times:
            times[col] = np.nan

    first = lambda ev, cols: (events.loc[events.ev == ev].drop_duplicates("clid")
                              .set_index("clid")[cols])
    orders = times.join(first("IN", ["gw", "mtype", "sym", "side", "qty"]))
    orders = orders.join(first("OUT", ["venue"]))
    orders = orders.join(first("REJ", ["reason"]))
    # gw / mtype are also recoverable from OUT when the IN line was truncated
    out_attrs = first("OUT", ["gw", "mtype"])
    orders["gw"] = orders["gw"].fillna(out_attrs["gw"])
    orders["mtype"] = orders["mtype"].fillna(out_attrs["mtype"])
    orders["gw"] = orders["gw"].fillna(events.drop_duplicates("clid").set_index("clid")["gw"])

    has = {c: orders[c].notna() for c in ["IN", "OUT", "ACK", "REJ", "ERR"]}
    orders["status"] = np.select(
        [has["ACK"] & has["REJ"],
         ~has["IN"],
         has["ERR"] & ~has["OUT"],
         ~has["OUT"],
         has["ACK"],
         has["REJ"]],
        ["CONFLICT", "INCOMPLETE_LOG", "INTERNAL_ERROR", "NOT_SENT", "ACKED", "REJECTED"],
        default="NO_RESPONSE",
    )

    orders["internal_us"] = (orders["OUT"] - orders["IN"]) / NS_PER_US
    resp = orders["ACK"].fillna(orders["REJ"])
    orders["rtt_us"] = (resp - orders["OUT"]) / NS_PER_US
    orders["total_us"] = (resp - orders["IN"]) / NS_PER_US
    orders["t_in"] = pd.to_datetime(orders["IN"].fillna(orders["OUT"]), unit="ns", utc=True)

    neg = int(((orders.internal_us < 0) | (orders.rtt_us < 0)).sum())
    print("\n== Part B: per-order table ==")
    print(f"orders: {len(orders):,}   negative latencies: {neg}  "
          f"(swapped lines don't matter because we pair by clid, not by file order)")
    print(orders["status"].value_counts().to_string())
    return orders


# ---------------------------------------------------------------------------
# Part C -- summary statistics
# ---------------------------------------------------------------------------
def pct_table(s: pd.Series) -> pd.Series:
    s = s.dropna()
    q = s.quantile([0.5, 0.9, 0.99, 0.999]).to_numpy()
    return pd.Series({"count": len(s), "mean": s.mean(), "p50": q[0], "p90": q[1],
                      "p99": q[2], "p99.9": q[3], "max": s.max()})


def part_c_stats(orders: pd.DataFrame) -> None:
    pd.set_option("display.width", 140)
    sent = orders[orders.OUT.notna() & orders.IN.notna()]
    print("\n== Part C: internal latency IN->OUT (us), by gateway x type ==")
    print(sent.groupby(["gw", "mtype"])["internal_us"].apply(pct_table).unstack().round(1))

    acked = orders[orders.status == "ACKED"]
    is_open = acked.t_in.dt.strftime("%H:%M:%S").between("14:30:00", "14:30:29")
    print("\n== Part C: venue RTT OUT->ACK (us), by venue (excl. open burst) ==")
    print(acked[~is_open].groupby("venue")["rtt_us"].apply(pct_table).unstack().round(1))
    print("\n   ... during the open burst 14:30:00-14:30:30 ==")
    print(acked[is_open].groupby("venue")["rtt_us"].apply(pct_table).unstack().round(1))

    gw3 = sent.loc[sent.gw == "gw-3", "internal_us"]
    print(f"\nmean vs median, gw-3 internal: mean={gw3.mean():.1f}us median={gw3.median():.1f}us "
          f"-> heavy right tail; the mean is dominated by a few outliers")


# ---------------------------------------------------------------------------
# Part D -- plots + root cause for the worst gateway
# ---------------------------------------------------------------------------
def part_d_plots(orders: pd.DataFrame) -> None:
    sent = orders[orders.OUT.notna() & orders.IN.notna()]
    gws = sorted(sent.gw.unique())

    # 1) log-x histogram (latency spans 3+ orders of magnitude -> linear bins are useless)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    bins = np.logspace(0, 4, 120)
    for gw in gws:
        ax.hist(sent.loc[sent.gw == gw, "internal_us"], bins=bins, histtype="step", lw=1.4, label=gw)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("internal latency IN->OUT (us, log)"); ax.set_ylabel("orders (log)")
    ax.set_title("Gateway internal latency distribution"); ax.legend()
    fig.tight_layout(); fig.savefig(PLOTS / "p01_internal_latency_hist.png", dpi=120); plt.close(fig)

    # 2) complementary ECDF on log-log: the tail is where the gateways differ
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for gw in gws:
        x = np.sort(sent.loc[sent.gw == gw, "internal_us"].to_numpy())
        ccdf = 1.0 - np.arange(len(x)) / len(x)
        ax.step(x, ccdf, where="post", label=gw)
    for p in (1e-2, 1e-3):
        ax.axhline(p, color="grey", ls=":", lw=0.8)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("internal latency (us)"); ax.set_ylabel("P(latency > x)")
    ax.set_title("Tail (1 - ECDF): dotted lines = p99 / p99.9"); ax.legend()
    fig.tight_layout(); fig.savefig(PLOTS / "p01_internal_latency_ccdf.png", dpi=120); plt.close(fig)

    # 3) worst gateway by p99: latency vs time, then "fold" time modulo a candidate period
    p99 = sent.groupby("gw")["internal_us"].quantile(0.99)
    worst = p99.idxmax()
    w = sent[sent.gw == worst].copy()
    slow = w[w.internal_us > 500]
    # Period detection: gaps between consecutive slow *clusters* (start seconds)
    cluster_starts = np.unique((slow["IN"] // NS_PER_S).to_numpy())
    gaps = np.diff(cluster_starts)
    period = int(pd.Series(gaps).mode().iloc[0]) if len(gaps) else None
    w["phase_ms"] = (w["IN"] % (period * NS_PER_S)) / 1e6 if period else np.nan
    slow_phase = w.loc[w.internal_us > 500, "phase_ms"]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].scatter(w.t_in, w.internal_us, s=2, alpha=0.4)
    axes[0].set_yscale("log"); axes[0].set_title(f"{worst}: internal latency over time")
    axes[0].set_ylabel("us (log)")
    axes[1].scatter(w.phase_ms, w.internal_us, s=2, alpha=0.4)
    axes[1].set_xscale("symlog", linthresh=100); axes[1].set_yscale("log")
    axes[1].set_xlabel(f"phase within {period}s cycle (ms, symlog)")
    axes[1].set_title(f"{worst}: folded at period={period}s")
    fig.autofmt_xdate(); fig.tight_layout()
    fig.savefig(PLOTS / "p01_worst_gateway_vs_time.png", dpi=120); plt.close(fig)

    print("\n== Part D: root cause ==")
    print("p99 internal latency by gateway (us):", p99.round(1).to_dict())
    print(f"worst gateway: {worst}; slow orders (>500us): {len(slow)} of {len(w):,} "
          f"({len(slow) / len(w):.2%})")
    print(f"most common gap between slow clusters: {period}s  (gap counts: "
          f"{dict(Counter(gaps.tolist()).most_common(3))})")
    print(f"slow orders' phase within the {period}s cycle: "
          f"{slow_phase.min():.0f}..{slow_phase.max():.0f} ms  "
          f"-> periodic stall at the start of every {period}s wall-clock boundary")
    print(f"{worst} p99 without the stall window: "
          f"{w.loc[w.phase_ms > slow_phase.max() + 1, 'internal_us'].quantile(0.99):.1f}us")


# ---------------------------------------------------------------------------
# Part E -- orders that never got a response
# ---------------------------------------------------------------------------
def part_e_missing(orders: pd.DataFrame, gap_s: float = 5.0) -> None:
    nr = orders[orders.status == "NO_RESPONSE"].sort_values("OUT")
    print("\n== Part E: missing responses ==")
    print(f"NO_RESPONSE: {len(nr)} orders "
          f"({len(nr) / orders.OUT.notna().sum():.2%} of sent); by venue:",
          nr.venue.value_counts().to_dict(), "by gw:", nr.gw.value_counts().to_dict())
    # Cluster in time: a new cluster starts when the gap to the previous one > gap_s
    t = nr["OUT"].to_numpy() / NS_PER_S
    cluster_id = np.concatenate([[0], np.cumsum(np.diff(t) > gap_s)])
    clusters = (nr.assign(cid=cluster_id).groupby("cid")
                .agg(n=("venue", "size"), start=("t_in", "min"), end=("t_in", "max"),
                     venues=("venue", lambda v: dict(Counter(v)))))
    big = clusters[clusters.n >= 10]
    for _, c in big.iterrows():
        print(f"  cluster: {c.n} orders {c.start:%H:%M:%S.%f}..{c.end:%H:%M:%S.%f} venues={c.venues}")
    # Share of ARCX orders lost inside that window (are *all* ARCX orders affected?)
    if len(big):
        c = big.iloc[0]
        win = orders[(orders.t_in >= c.start) & (orders.t_in <= c.end) & orders.OUT.notna()]
        print("  fraction lost inside window by venue:",
              win.groupby("venue")["status"].apply(lambda s: round((s == "NO_RESPONSE").mean(), 2)).to_dict())
    print(f"  scattered (not in a cluster): {len(nr) - int(big.n.sum())}")

    internal = orders[orders.status.isin(["INTERNAL_ERROR", "NOT_SENT"])]
    print(f"INTERNAL_ERROR/NOT_SENT (IN but no OUT): {len(internal)} "
          f"(ERROR lines with traceback: {int(orders.ERR.notna().sum())})")
    print(f"INCOMPLETE_LOG (IN line truncated/lost): {(orders.status == 'INCOMPLETE_LOG').sum()}")

    rej = orders[orders.status == "REJECTED"]
    print("rejects by reason x gw:")
    print(pd.crosstab(rej.reason, rej.gw).to_string())
    rl = rej[rej.reason == "RATE_LIMIT"]
    print(f"RATE_LIMIT time range: {rl.t_in.min():%H:%M:%S} .. {rl.t_in.max():%H:%M:%S}")

    # timeline plot: per-5s p99 RTT per venue + no-response counts
    acked = orders[orders.status == "ACKED"]
    p99_t = acked.set_index("t_in").groupby("venue")["rtt_us"].resample("5s").quantile(0.99).unstack(0)
    nr_t = nr.set_index("t_in").resample("5s")["venue"].count()
    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    for venue in p99_t.columns:          # plain matplotlib (pandas' date axis + bar don't mix)
        ax[0].plot(p99_t.index, p99_t[venue], label=venue)
    ax[0].legend(); ax[0].set_ylabel("p99 RTT (us), 5s bins"); ax[0].set_title("Venue RTT over time")
    ax[1].bar(nr_t.index, nr_t.values, width=5 / 86400, align="edge", color="tab:red")
    ax[1].set_ylabel("no-response orders / 5s")
    fig.tight_layout(); fig.savefig(PLOTS / "p01_venue_rtt_timeline.png", dpi=120); plt.close(fig)


# ---------------------------------------------------------------------------
# Part F -- scale: streaming pairing + mergeable log-bucket histogram
# ---------------------------------------------------------------------------
class LogHistogram:
    """HDR-style histogram: buckets with ~1% relative width. Mergeable by adding counts,
    O(#buckets) memory regardless of #samples. Percentile error <= bucket width."""

    def __init__(self, rel_precision: float = 0.01):
        self.log_base = math.log1p(rel_precision)
        self.counts: Counter = Counter()
        self.n = 0

    def add(self, value_us: float) -> None:
        self.counts[int(math.log(max(value_us, 1e-3)) / self.log_base)] += 1
        self.n += 1

    def merge(self, other: "LogHistogram") -> "LogHistogram":
        self.counts.update(other.counts)
        self.n += other.n
        return self

    def quantile(self, q: float) -> float:
        target, seen = q * self.n, 0
        for b in sorted(self.counts):
            seen += self.counts[b]
            if seen >= target:
                return math.exp((b + 0.5) * self.log_base)   # bucket midpoint
        return float("nan")


def part_f_streaming(path: Path, orders: pd.DataFrame) -> None:
    """Single pass, memory O(in-flight orders + buckets): what you'd run per gateway
    shard on 200 GB/day. Pairs IN/OUT by clid in either file order."""
    pending_in, pending_out = {}, {}
    # Duplicate lines would re-open an already-matched order. Remember recently
    # completed clids in a bounded FIFO (dups are close together in the file).
    recently_done: OrderedDict[str, None] = OrderedDict()
    dup_skipped = 0
    hist_by_gw: dict[str, LogHistogram] = {}
    for ev in iter_events(path, Counter()):
        ts, gw, kind, clid = ev[0], ev[1], ev[4], ev[6]
        if kind in ("IN", "OUT") and clid in recently_done:
            dup_skipped += 1
            continue
        if kind == "IN":
            t_out = pending_out.pop(clid, None)      # OUT already seen (swapped lines)
            if t_out is None:
                pending_in[clid] = ts
                continue
            lat = t_out - ts
        elif kind == "OUT":
            t_in = pending_in.pop(clid, None)
            if t_in is None:
                pending_out[clid] = ts
                continue
            lat = ts - t_in
        else:
            continue
        hist_by_gw.setdefault(gw, LogHistogram()).add(lat / NS_PER_US)
        recently_done[clid] = None
        if len(recently_done) > 50_000:
            recently_done.popitem(last=False)          # O(1) eviction of the oldest

    merged = LogHistogram()
    for h in hist_by_gw.values():
        merged.merge(h)
    exact = orders.internal_us.dropna()
    naive_avg_p99 = np.mean([h.quantile(0.99) for h in hist_by_gw.values()])
    print("\n== Part F: streaming + mergeable histograms ==")
    print(f"duplicate IN/OUT skipped: {dup_skipped}; unmatched after stream: "
          f"IN={len(pending_in)} (never sent) OUT={len(pending_out)} (IN line lost)")
    print(f"global p99: exact={exact.quantile(0.99):.1f}us  merged-histogram={merged.quantile(0.99):.1f}us  "
          f"WRONG mean-of-per-gateway-p99={naive_avg_p99:.1f}us")
    print(f"histogram buckets used: {len(merged.counts)} (vs {merged.n:,} samples)")


def main() -> None:
    t0 = time.perf_counter()
    events = part_a_parse()
    orders = part_b_orders(events)
    part_c_stats(orders)
    part_d_plots(orders)
    part_e_missing(orders)
    part_f_streaming(LOG_PATH, orders)
    print(f"\ntotal runtime {time.perf_counter() - t0:.1f}s; plots in {PLOTS}")


if __name__ == "__main__":
    main()
