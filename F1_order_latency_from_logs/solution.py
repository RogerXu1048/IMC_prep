#!/usr/bin/env python3
"""F1 — order processing latency from gateway logs.  Run: python3 make_data.py && python3 solution.py"""
from __future__ import annotations

import re
from collections import Counter
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
NS_PER_US, NS_PER_S = 1_000, 1_000_000_000
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.(\d{6}|\d{9}) (gw-\d+) (\w+)\s+\[t\d+\] (.*)$")
CONTINUATION_RE = re.compile(r"^(\s|Traceback|[\w.]+: )")      # traceback lines under an ERROR line
REQUIRED = {("IN", "NewOrder"): {"clid", "qty"}, ("IN", "Cancel"): {"clid", "sym"},
            ("OUT", "NewOrder"): {"clid", "venue"}, ("OUT", "Cancel"): {"clid", "venue"},
            ("ACK", None): {"clid", "exch_id"}, ("REJ", None): {"clid", "reason"}}
VENUES = {"XNAS", "ARCX", "BATS"}
COLS = ["ts_ns", "gw", "ev", "mtype", "clid", "venue", "reason"]
OPEN_WINDOW = ("14:30:00", "14:30:29")       # US cash open = 14:30 UTC in November
SLOW_US = 500                                # "obviously slow" for a ~8 us process
CLUSTER_GAP_S = 5                            # no-response orders > 5 s apart = separate incidents


# ---------------------------------------------------------------- step 1: parse
@lru_cache(maxsize=None)
def epoch_s(date: str, hms: str) -> int:
    # DECISION: cache per second -- 15 min of logs has only ~900 distinct seconds and strptime is slow.
    return int(datetime.strptime(f"{date} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def parse_lines(path: Path, dq: Counter) -> Iterator[tuple]:
    # DECISION: generator -> O(1) memory per line; the production log is ~200 GB/day.
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            dq["lines"] += 1
            m = LINE_RE.match(line.rstrip("\n"))
            if m is None:
                dq["traceback" if CONTINUATION_RE.match(line) else "malformed"] += 1
                continue
            date, hms, frac, gw, level, body = m.groups()
            tokens = body.split()                 # TRADE-OFF: split is faster than one big regex, less strict
            ev = tokens[0]
            if ev not in ("IN", "OUT", "ACK", "REJ"):
                dq["error_lines" if level == "ERROR" else "heartbeat_stats"] += 1
                continue
            mtype = tokens[1] if ev in ("IN", "OUT") else None
            kv = dict(t.split("=", 1) for t in tokens if "=" in t)
            # CHECK: a truncated line loses its last field(s) -> drop it rather than trust half a record.
            if not REQUIRED.get((ev, mtype), {"?"}) <= kv.keys() or (ev == "OUT" and kv["venue"] not in VENUES):
                dq["malformed"] += 1
                continue
            # DECISION: normalise 6-digit (us) and 9-digit (ns) fractions to integer ns, never floats.
            frac_ns = int(frac) if len(frac) == 9 else int(frac) * NS_PER_US
            dq["events"] += 1
            yield epoch_s(date, hms) * NS_PER_S + frac_ns, gw, ev, mtype, kv["clid"], kv.get("venue"), kv.get("reason")


def step1_parse(path: Path = LOG_PATH) -> tuple[pd.DataFrame, Counter]:
    dq: Counter = Counter()
    ev = pd.DataFrame.from_records(parse_lines(path, dq), columns=COLS)
    dq["out_of_order"] = int((np.diff(ev.ts_ns.to_numpy()) < 0).sum())   # multi-threaded logger
    dup = ev.duplicated(["gw", "clid", "ev", "ts_ns"])                    # logger retried a write
    dq["duplicates"] = int(dup.sum())
    # CHECK: keep timestamps as Int64 -- epoch-ns in float64 only resolves ~256 ns.
    return ev[~dup].astype({"ts_ns": "Int64"}), dq


# ---------------------------------------------------------------- step 2: one row per order
def step2_orders(ev: pd.DataFrame) -> pd.DataFrame:
    # DECISION: pair by clid (a hash join), never by file order -- lines are out of order.
    t = ev.groupby(["clid", "ev"]).ts_ns.min().unstack().reindex(columns=["IN", "OUT", "ACK", "REJ"])
    o = t.join(ev.groupby("clid")[["gw", "mtype", "venue", "reason"]].first())   # first non-null
    o["status"] = np.select([o.IN.isna(), o.OUT.isna(), o.ACK.notna(), o.REJ.notna()],
                            ["INCOMPLETE_LOG", "NOT_SENT", "ACKED", "REJECTED"], "NO_RESPONSE")
    o["internal_us"] = ((o.OUT - o.IN) / NS_PER_US).astype(float)          # time inside our gateway
    o["rtt_us"] = ((o.ACK.fillna(o.REJ) - o.OUT) / NS_PER_US).astype(float)  # network + exchange
    o["t_out"] = pd.to_datetime(o.OUT, unit="ns", utc=True)
    # CHECK: a negative latency would mean a pairing or clock bug.
    assert not ((o.internal_us < 0) | (o.rtt_us < 0)).any()
    return o


# ---------------------------------------------------------------- step 3: statistics
def pct(s: pd.Series) -> pd.Series:
    s = s.dropna()
    q = s.quantile([0.5, 0.99, 0.999])
    return pd.Series({"count": len(s), "mean": s.mean(), "p50": q[0.5], "p99": q[0.99],
                      "p99.9": q[0.999], "max": s.max()})


def step3_stats(o: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    internal = o.dropna(subset=["internal_us"]).groupby(["gw", "mtype"]).internal_us.apply(pct).unstack()
    acked = o[o.status == "ACKED"]
    # DECISION: split the open burst out -- 5x message rate, a different regime, not "normal" RTT.
    period = np.where(acked.t_out.dt.strftime("%H:%M:%S").between(*OPEN_WINDOW), "open", "normal")
    rtt = acked.groupby(["venue", period]).rtt_us.apply(pct).unstack()
    return internal.round(1), rtt.round(1)


# ---------------------------------------------------------------- step 4: diagnose
def step4_diagnose(o: pd.DataFrame) -> dict:
    sent = o.dropna(subset=["internal_us"])
    p99 = sent.groupby("gw").internal_us.quantile(0.99)
    worst = p99.idxmax()
    w = sent[sent.gw == worst]
    t_in = w.IN.astype("int64")
    slow = w.internal_us > SLOW_US
    # DECISION: period = most common gap (s) between seconds that contain slow orders.
    period_s = int(pd.Series(np.diff(np.unique(t_in[slow] // NS_PER_S))).mode()[0])
    phase_ms = (t_in % (period_s * NS_PER_S)) / 1e6
    in_window = phase_ms <= phase_ms[slow].max()

    nr = o[o.status == "NO_RESPONSE"].sort_values("OUT")
    cluster_id = (nr.OUT.diff() > CLUSTER_GAP_S * NS_PER_S).cumsum()    # gap-based clustering
    clusters = nr.groupby(cluster_id).agg(n=("venue", "size"), start=("t_out", "min"), end=("t_out", "max"),
                                          venue=("venue", lambda v: v.value_counts().idxmax()))
    rate_limit = o[o.reason == "RATE_LIMIT"]

    fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
    for gw, g in sent.groupby("gw"):
        x = np.sort(g.internal_us.to_numpy())
        ax[0].step(x, 1 - np.arange(len(x)) / len(x), where="post", label=gw)
    ax[0].set(xscale="log", yscale="log", xlabel="internal latency (us)", ylabel="P(latency > x)",
              title="tail by gateway")
    ax[0].legend()
    ax[1].scatter(phase_ms, w.internal_us, s=2)
    ax[1].set(yscale="log", xlabel=f"ms into each {period_s}s cycle", title=f"{worst}: folded by time")
    pd.crosstab(nr.t_out.dt.floor("5s"), nr.venue).plot(ax=ax[2], marker="o", lw=0,
                                                        title="no-response orders per 5 s")
    fig.tight_layout()
    fig.savefig(PLOTS / "f1_diagnosis.png", dpi=110)
    plt.close(fig)
    return {"p99_by_gw": p99.round(1).to_dict(), "worst": worst, "slow_share": slow.mean(),
            "period_s": period_s, "slow_phase_ms": (phase_ms[slow].min(), phase_ms[slow].max()),
            "worst_p99_outside_window": w.internal_us[~in_window].quantile(0.99),
            "no_response": len(nr), "clusters": clusters[clusters.n >= 10],
            "rate_limit_by_gw": rate_limit.gw.value_counts().to_dict(),
            "rate_limit_window": (rate_limit.t_out.min(), rate_limit.t_out.max())}


# ---------------------------------------------------------------- step 5: scale (mergeable p99)
def step5_merge_p99(o: pd.DataFrame) -> tuple[float, float, float]:
    # DECISION: each shard ships a log-bucket histogram (HDR-style); merging = adding counts.
    # TRADE-OFF: bucket width (~2.3%) bounds the quantile error, memory is O(#buckets) not O(#orders).
    sent = o.dropna(subset=["internal_us"])
    bins = np.logspace(0, 5, 501)                                    # 1 us .. 100 ms
    merged = sum(np.histogram(g.internal_us, bins)[0] for _, g in sent.groupby("gw"))
    merged_p99 = bins[np.searchsorted(np.cumsum(merged) / merged.sum(), 0.99) + 1]
    wrong_avg = sent.groupby("gw").internal_us.quantile(0.99).mean()  # percentiles don't average
    return sent.internal_us.quantile(0.99), merged_p99, wrong_avg


def main() -> None:
    pd.set_option("display.width", 140)
    PLOTS.mkdir(exist_ok=True)
    ev, dq = step1_parse()
    print("== step 1: data quality ==", dict(dq))
    print("gateways logging ns timestamps:", sorted(ev.loc[ev.ts_ns % NS_PER_US != 0, "gw"].unique()))

    o = step2_orders(ev)
    print("\n== step 2: orders by status ==", o.status.value_counts().to_dict())

    internal, rtt = step3_stats(o)
    print("\n== step 3: internal latency IN->OUT (us) ==\n", internal)
    print("\n== step 3: venue RTT OUT->ACK (us) ==\n", rtt)
    g3 = o.loc[o.gw == "gw-3", "internal_us"]
    print(f"gw-3 mean={g3.mean():.1f}us vs median={g3.median():.1f}us -> the mean is driven by the tail")

    d = step4_diagnose(o)
    print("\n== step 4: diagnosis ==")
    print("p99 internal by gateway (us):", d["p99_by_gw"])
    print(f"{d['worst']}: {d['slow_share']:.2%} of orders > {SLOW_US}us; period = {d['period_s']}s; "
          f"slow orders sit {d['slow_phase_ms'][0]:.0f}-{d['slow_phase_ms'][1]:.0f} ms into each cycle; "
          f"p99 outside that window = {d['worst_p99_outside_window']:.1f}us")
    print(f"no-response orders: {d['no_response']}; clusters of >= 10:\n{d['clusters'].to_string(index=False)}")
    print(f"RATE_LIMIT rejects by gw: {d['rate_limit_by_gw']}, "
          f"{d['rate_limit_window'][0]:%H:%M:%S}-{d['rate_limit_window'][1]:%H:%M:%S}")

    exact, merged, wrong = step5_merge_p99(o)
    print(f"\n== step 5: global p99 exact={exact:.1f}us, merged histograms={merged:.1f}us, "
          f"WRONG average of per-gateway p99s={wrong:.1f}us")


if __name__ == "__main__":
    main()
