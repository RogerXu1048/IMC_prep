"""F1 -- order latency from gateway logs.  Run: python3 make_data.py && python3 solution.py"""
import math
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LOG, PLOTS = ROOT / "data" / "p01" / "gateway.log", ROOT / "plots"
# One regex per order-event line, anchored at both ends, so a line cut short by log rotation fails to match.
EVENT_RE = re.compile(r"^\S+ (\d\d):(\d\d):(\d\d)\.(\d{6}|\d{9}) (gw-\d) +\w+ +\[t\d\] (IN|OUT|ACK|REJ) +"
                      r"(?:(NewOrder|Cancel) )?clid=(\S+) (?:venue=(XNAS|ARCX|BATS)|reason=([A-Z_]+)|.*=\w+)$")
NS, MS, SLOW_US, CYCLE_S = 10**9, 10**6, 500, 10   # SLOW: normal < ~30 us, slow 1-4 ms; CYCLE: CHECKed in step 4
TICKS = [1, 3, 10, 30, 100, 300, 1000, 3000]
PER_DEC, NB = 50, 200                                  # step 5: 50 log bins per decade, 1 us .. 10 ms (~5% wide)
hms = lambda ns: pd.Timestamp(int(ns)).strftime("%H:%M:%S")      # ns since midnight -> "HH:MM:SS"
to_ns = lambda h, mi, s, frac: ((int(h) * 60 + int(mi)) * 60 + int(s)) * NS + int(frac.ljust(9, "0"))


def load():
    rows, dq = [], dict.fromkeys(["malformed", "traceback", "ERROR", "HB/stats"], 0)
    with open(LOG) as f:
        for line in f:
            if m := EVENT_RE.match(line):
                rows.append((to_ns(*m.groups()[:4]), *m.groups()[4:]))   # DECISION: int ns, us padded. Never float.
            elif not line[:1].isdigit():       # indented lines under an ERROR
                dq["traceback"] += 1
            elif " ERROR " in line:
                dq["ERROR"] += 1
            elif " HB " in line or "session stats" in line:
                dq["HB/stats"] += 1
            else:                              # truncated line: drop it rather than trust half a record
                dq["malformed"] += 1
    ev = pd.DataFrame(rows, columns=["ts", "gw", "ev", "mtype", "clid", "venue", "reason"])
    back, dup = -np.diff(ev.ts), ev.duplicated()   # back > 0: a line older than the one before it
    print(f"STEP 1 audit: {len(ev) + sum(dq.values())} lines -> {len(ev)} order events | skipped {dq}\n  dropped "
          f"{dup.sum()} exact duplicates | kept {(back > 0).sum()} out-of-order lines (<= {back.max() / MS:.1f} ms "
          f"back; we pair by clid) | ns timestamps: {sorted(ev.gw[ev.ts % 1000 != 0].unique())}")
    return ev[~dup]


def per_order(ev):
    # DECISION: pair by clid, never by file order. Int64 keeps ns exact where an event is missing (float would not).
    t = ev.astype({"ts": "Int64"}).pivot_table(index="clid", columns="ev", values="ts", aggfunc="min")
    o = t.join(ev.groupby("clid")[["gw", "mtype", "venue", "reason"]].first())
    o["internal_us"] = ((o.OUT - o.IN) / 1e3).astype(float)            # inside our gateway, one host clock
    o["rtt_us"] = ((o.ACK.fillna(o.REJ) - o.OUT) / 1e3).astype(float)  # wire + venue
    o["status"] = np.select([o.ACK.notna(), o.REJ.notna(), o.OUT.isna()], ["acked", "rejected", "not sent"],
                            default="no response")
    assert not ((o.internal_us < 0) | (o.rtt_us < 0)).any()             # CHECK: negative = pairing/clock bug
    return o


def stats(o):
    sent = o.dropna(subset=["internal_us"])
    g = sent.groupby(["gw", "mtype"]).internal_us
    internal = g.quantile([0.5, 0.99]).unstack().assign(mean=g.mean()).round(1)
    per_s = (o.OUT.dropna() // NS).value_counts()                    # orders sent in each second
    burst = per_s[per_s > 3 * per_s.median()]   # DECISION: split out this burst (the confounder). CHECK: = US open?
    acked = o[o.status == "acked"]
    g = acked.groupby(["venue", np.where((acked.OUT // NS).isin(burst.index), "open", "normal")]).rtt_us
    rtt = g.quantile([0.5, 0.99]).unstack().assign(n=g.count()).round(1)
    p99 = sent.groupby("gw").internal_us.quantile(0.99)
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for gw, x in sent.groupby("gw").internal_us:                      # 1 - ECDF on log-log shows the tail
        ax.step(np.sort(x), 1 - np.arange(len(x)) / len(x), where="post", label=gw)
    ax.set(xscale="log", yscale="log", xlabel="internal latency IN->OUT (µs, log)", ylabel="share of orders slower",
           title=f"{p99.idxmax()}: same median, but p99 = {p99.max():.0f} µs vs ~{p99.median():.0f} µs elsewhere")
    ax.set_xticks(TICKS, TICKS), ax.set_yticks([1, 0.1, 0.01, 1e-3, 1e-4], ["100%", "10%", "1%", "0.1%", "0.01%"])
    ax.minorticks_off(), ax.legend(), fig.tight_layout(), fig.savefig(PLOTS / "f1_latency_tail.png", dpi=110)
    return internal, rtt, burst, per_s.median(), p99


def diagnose(o, worst):
    w = o[(o.gw == worst) & o.internal_us.notna()].assign(t=lambda d: d.IN.astype("int64"))
    phase_ms, slow = (w.t % (CYCLE_S * NS)) / MS, w.internal_us > SLOW_US      # phase = ms into the cycle
    stall_ms = phase_ms[slow].max()
    nr = o[o.status == "no response"].assign(t=lambda d: d.OUT.astype("int64")).sort_values("t")
    nr["incident"] = (nr.t.diff() > 5 * NS).cumsum()                   # DECISION: a > 5 s gap starts a new incident
    big, rl = nr[nr.groupby("incident").t.transform("size") >= 10], o[o.reason == "RATE_LIMIT"]
    top = big.venue.value_counts()
    r = dict(worst=worst, slow_pct=100 * slow.mean(), stall_ms=stall_ms, n_nr=len(nr), n_big=len(big),
             gap_s=pd.Series(np.diff(np.unique(w.t[slow] // NS))).mode()[0],   # CHECK: should equal CYCLE_S
             clean_p99=w.internal_us[phase_ms > stall_ms].quantile(0.99), venues=top.to_dict(), venue=top.index[0],
             n_venue=top.iloc[0], n_gw=big.gw.nunique(), start=hms(big.t.min()), secs=(big.t.max() - big.t.min()) / NS,
             rl_gw=rl.gw.value_counts().to_dict(), rl_when=f"{hms(rl.OUT.min())}-{hms(rl.OUT.max())}")
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.3))
    ax[0].scatter(phase_ms, w.internal_us, s=2, alpha=0.2, rasterized=True)
    ax[0].set(yscale="log", xlabel=f"ms into each {CYCLE_S} s cycle", ylabel="internal latency (µs, log)",
              title=f"{worst}: every order > {SLOW_US} µs is in the first {stall_ms:.0f} ms of a {CYCLE_S} s cycle")
    ax[0].set_yticks(TICKS, TICKS), ax[0].minorticks_off()
    ax[1].scatter(pd.to_datetime(nr.t), nr.venue, marker="|", s=300, alpha=0.4)   # one tick per unanswered order
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax[1].set(xlabel="time the order was sent (UTC)", ylabel="venue",
              title="{venue}: {n_venue} orders got no answer in {secs:.0f} s from {start}".format(**r))
    fig.tight_layout(), fig.savefig(PLOTS / "f1_diagnosis.png", dpi=110)
    return r


def gw_hist(gw):
    """STEP 5 worker: stream ONE gateway's lines. Memory = open orders + NB counters, not the whole file."""
    open_, h = {}, np.zeros(NB, int)
    with open(LOG) as f:                     # here all gateways share one file; in production each has its own
        for line in f:
            if f" {gw} " in line and (m := EVENT_RE.match(line)) and m[6] in ("IN", "OUT"):  # cheap test first
                ts, ev, clid = to_ns(*m.groups()[:4]), m[6], m[8]
                first = open_.setdefault(clid, (ev, ts))     # OUT can be logged before IN: keep whichever is first
                if first[0] != ev:                           # pair complete -> one sample, free the memory
                    us = (ts - first[1] if ev == "OUT" else first[1] - ts) / 1e3
                    h[min(max(int(PER_DEC * math.log10(max(us, 1))), 0), NB - 1)] += 1   # O(1) log bin
                    del open_[clid]
    return h, len(open_)                     # left open: never sent, or a duplicate line after the pair closed


def scale_out(o):
    """STEP 5: 200 GB/day won't fit in a DataFrame. One worker per gateway file; each returns a tiny histogram."""
    t, gws = time.perf_counter(), sorted(o.gw.unique())
    with ProcessPoolExecutor() as ex:        # DECISION: processes, not threads: regex parsing is CPU-bound (GIL)
        res = dict(zip(gws, ex.map(gw_hist, gws)))
    p99 = lambda h: 10 ** ((np.searchsorted(np.cumsum(h), 0.99 * h.sum()) + 1) / PER_DEC)   # bin's upper edge
    merged = sum(h for h, _ in res.values())   # DECISION: ship histograms and ADD them; p99s cannot be averaged
    assert merged.sum() == o.internal_us.notna().sum()                 # CHECK: streaming pairs = step-2 pairs
    return dict(wall_s=time.perf_counter() - t, merged=p99(merged), exact=o.internal_us.quantile(0.99), n=merged.sum(),
                avg=np.mean([p99(h) for h, _ in res.values()]), left=sum(n for _, n in res.values()))


def main():
    PLOTS.mkdir(exist_ok=True)
    t = time.perf_counter()
    o = per_order(load())
    parse_s = time.perf_counter() - t
    print("\nSTEP 2 orders by status:", o.status.value_counts().to_dict())
    internal, rtt, burst, base, p99 = stats(o)
    print(f"\nSTEP 3 internal latency IN->OUT (us):\n{internal}\n  venue round trip OUT->ACK (us), open burst "
          f"{hms(burst.index.min() * NS)}-{hms(burst.index.max() * NS)} ({burst.mean():.0f}/s vs {base:.0f}/s):\n{rtt}")
    r = diagnose(o, p99.idxmax())
    print("\nSTEP 4 stall: {worst} has {slow_pct:.2f}% of orders > 500 us, every {gap_s} s, all in the first "
          "{stall_ms:.0f} ms of the cycle; p99 outside it {clean_p99:.1f} us\n  no response: {n_nr} orders; {n_big} in "
          "{secs:.0f} s from {start}: {venues} from {n_gw} gateways\n  RATE_LIMIT: {rl_gw}, {rl_when}".format(**r))
    s = scale_out(o)
    print("\nSTEP 5 scale: 1 process parsed + paired in {p:.1f} s; {w} streaming processes took {wall_s:.1f} s\n"
          "  overall p99: merged histogram <= {merged:.1f} us (exact {exact:.1f} us, {n:,} orders); averaging the "
          "gateway p99s says {avg:.0f} us -- wrong\n  left open at the end: {left} (evict after a TTL in production)"
          .format(**s, p=parse_s, w=o.gw.nunique()))
    p50 = internal[0.5].groupby(level="mtype").median()
    print("\nCONCLUSION\n - Typical internal latency: {NewOrder:.0f} us NewOrder / {Cancel:.0f} us Cancel on all four;"
          " quote p50 + p99 per gateway, not a mean.\n - {worst} stalls ~{stall_ms:.0f} ms every {gap_s} s; outside the"
          " stall its p99 is {clean_p99:.0f} us like the rest. A periodic job on that host?\n - {venue} went silent at "
          "{start}: {n_venue} orders in unknown state (reconcile, don't resend). {rl_top} hit RATE_LIMIT at the open.\n"
          " - Fleet scale: stream each gateway, ship a histogram, add them (p99 {merged:.0f} us); never average p99s "
          "({avg:.0f} us).".format(**r, **p50, **s, rl_top=list(r["rl_gw"])[0]))
    print("LIMITATIONS\n - One 15-minute window on one day: the 10 s cycle and the venue gap need more days to confirm."
          "\n - Log time is when a line was written, not when the event happened. IN->OUT is same-host; cross-host "
          "needs PTP.\n - The stall's cause is inferred from timing, not proven: check that host's cron and perf data.")


if __name__ == "__main__":
    main()
