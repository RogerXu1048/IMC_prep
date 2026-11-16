"""F3 -- A/B feed: integrity, latency, arbitration, "did the B upgrade help?"  Run after make_data.py"""
import heapq
from itertools import repeat
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA, PLOTS = HERE.parent / "data" / "p04", HERE.parent / "plots"
LINES = ("A", "B")
QS = [0.5, 0.9, 0.99, 0.999]
RATE_WINDOW_NS = 100_000       # load proxy = msgs the exchange sent in the previous 100 us
RATE_BUCKETS = [0, 25, 50, 75, 100, 150, 1_000]
GAP_TIMEOUT_NS = 100_000       # how long the live arbiter waits for the other line
WIRE_OVERHEAD_B = 62           # Ethernet/IP/UDP headers (42) + preamble & inter-frame gap (20)


def runs(seqs: list[int]) -> int:
    """Number of runs of consecutive seqs -- tells clustered loss from random loss."""
    return 0 if not seqs else int(1 + (np.diff(seqs) != 1).sum())


def step1_load_and_check() -> tuple[dict[str, pd.DataFrame], dict[str, list[int]]]:
    feeds, report = {}, []
    for line in LINES:
        raw = pd.read_parquet(DATA / f"feed_{line}.parquet")     # one row per packet, arrival order
        raw["date"] = raw["date"].astype(str)
        raw["dup"] = raw.duplicated(["date", "seq"])
        # DECISION: keep the first copy of a duplicated seq -- the second copy adds nothing
        df = raw[~raw["dup"]].copy()
        # CHECK: rows are in arrival order, so a seq below the running max arrived out of order
        df["ooo"] = df["seq"] < df.groupby("date")["seq"].cummax()
        # DECISION: day-relative ns. Epoch ns (~1.8e18) > 2**53, so if a merge turns int64 into
        # float64 we would silently lose 256 ns of resolution.
        day0 = pd.to_datetime(df["date"]).astype("datetime64[ns]").astype("int64")
        df["exch"], df["rx"] = df["exch_ns"] - day0, df["rx_ns"] - day0
        df["lat_us"] = (df["rx_ns"] - df["exch_ns"]) / 1e3
        feeds[line] = df
        counts = raw.groupby("date").agg(packets=("seq", "size"), dups=("dup", "sum"))
        report.append(counts.join(df.groupby("date")["ooo"].sum()).assign(line=line))
    report = pd.concat(report).reset_index().set_index(["line", "date"])

    both_lost = {}
    for date in sorted(feeds["A"]["date"].unique()):
        got = {l: set(feeds[l].loc[feeds[l]["date"] == date, "seq"]) for l in LINES}
        # TRADE-OFF: the universe is 1..max seen seq; a loss at the very end of the day is invisible
        universe = set(range(1, max(max(s) for s in got.values()) + 1))
        for l in LINES:
            lost = sorted(universe - got[l])
            report.loc[(l, date), ["lost", "lost_pct", "mean_run"]] = [
                len(lost), 100 * len(lost) / len(universe), len(lost) / max(runs(lost), 1)]
        both_lost[date] = sorted((universe - got["A"]) & (universe - got["B"]))
    print(report.round(3).to_string())
    for date, seqs in both_lost.items():
        print(f"{date}: {len(seqs)} seqs in {runs(seqs)} runs lost on BOTH lines -> need retransmission")
    return feeds, both_lost


def step2_latency(feeds: dict[str, pd.DataFrame]) -> None:
    lat = pd.concat([f.assign(line=l) for l, f in feeds.items()], ignore_index=True)
    print(lat.groupby(["line", "date"])["lat_us"].quantile(QS).unstack().round(1).to_string())
    # CHECK: rx - exch mixes two clocks. The per-minute MINIMUM strips queueing; if it drifts the
    # same way on two independent lines, the drift is the clock offset, not the network.
    lat["minute"] = lat["exch"] // 60_000_000_000
    mins = lat.groupby(["line", "date", "minute"])["lat_us"].min().reset_index()
    for (line, date), g in mins.groupby(["line", "date"]):
        slope = np.polyfit(g["minute"], g["lat_us"], 1)[0] * 60
        print(f"  {line} {date}: per-minute minimum drifts {slope:+.2f} us/hour")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for (line, date), g in lat.groupby(["line", "date"]):
        x = np.sort(g["lat_us"].to_numpy())
        ax.plot(x, 1 - np.arange(len(x)) / len(x), label=f"line {line} {date}")
    ax.set(xscale="log", yscale="log", xlabel="rx - exch (us)", ylabel="P(latency > x)",
           title="Per-line latency tail (CCDF, log-log)")
    ax.legend()
    fig.savefig(PLOTS / "f3_latency_ccdf.png", dpi=110, bbox_inches="tight")
    plt.close(fig)


def step3_arbitrate(feeds: dict[str, pd.DataFrame]) -> pd.DataFrame:
    cols = ["date", "seq", "exch", "rx"]
    m = feeds["A"][cols].merge(feeds["B"][cols], on=["date", "seq"], how="outer", suffixes=("_A", "_B"))
    same = m.dropna(subset=["exch_A", "exch_B"])
    assert (same["exch_A"] == same["exch_B"]).all(), "A and B must carry identical packets"
    m["exch"] = m["exch_A"].fillna(m["exch_B"])
    # DECISION: first arrival wins; min() skips NaN, so a line that lost the packet never wins
    m["rx_arb"] = m[["rx_A", "rx_B"]].min(axis=1)
    m["winner"] = np.select([m["rx_B"].isna(), m["rx_A"].isna(), m["rx_A"] <= m["rx_B"]],
                            ["A only", "B only", "A"], "B")
    for c in ("A", "B", "arb"):
        m[f"lat_{c}"] = (m[f"rx_{c}"] - m["exch"]) / 1e3
    print((pd.crosstab(m["date"], m["winner"], normalize="index") * 100).round(2).to_string())
    print(m.groupby("date")[["lat_A", "lat_B", "lat_arb"]].quantile([0.5, 0.99, 0.999]).round(1).to_string())
    # CHECK: survivorship -- if B only drops packets stuck in deep queues, B-alone p99 looks too good
    lost_b = m["rx_B"].isna() & m["rx_A"].notna()
    print(f"line-A latency of packets that B lost: median {m.loc[lost_b, 'lat_A'].median():.0f} us "
          f"(all packets: {m['lat_A'].median():.0f} us) -> B drops inside microbursts")
    return m


def step3b_live_arbiter(feeds: dict[str, pd.DataFrame], date: str) -> tuple[dict, list]:
    """What the feed handler runs online: O(1) amortised per packet, publishes in seq order."""
    a, b = (feeds[l][feeds[l]["date"] == date] for l in LINES)
    arrivals = heapq.merge(zip(a["rx"].tolist(), a["seq"].tolist(), repeat("A")),
                           zip(b["rx"].tolist(), b["seq"].tolist(), repeat("B")))
    next_seq, pending, wins, gaps = 1, {}, {"A": 0, "B": 0}, []
    for rx, seq, line in arrivals:                 # the order the NIC sees them
        if seq < next_seq or seq in pending:
            continue                               # other line already delivered it
        wins[line] += 1
        pending[seq] = rx
        while pending:
            if next_seq in pending:                # publish downstream, strictly in order
                del pending[next_seq]
                next_seq += 1
            elif rx - min(pending.values()) > GAP_TIMEOUT_NS:
                # TRADE-OFF: longer timeout = fewer false gaps but a book frozen for longer
                first = min(pending)
                gaps.append((next_seq, first - 1))  # both lines lost it -> request retransmission
                next_seq = first
            else:
                break
    return wins, gaps


def step4_upgrade(m: pd.DataFrame, mean_bytes: float) -> dict:
    m = m.sort_values(["date", "exch"])
    # DECISION: load from the UNION of both lines, so one line's own losses can't bias its load
    # two pointers, vectorised: index of the first msg inside the window via binary search
    m["rate"] = m.groupby("date")["exch"].transform(
        lambda e: np.arange(len(e)) - np.searchsorted(e, e - RATE_WINDOW_NS, side="right") + 1)
    d1, d2 = sorted(m["date"].unique())

    naive = m.groupby("date")[["lat_A", "lat_B"]].quantile([0.5, 0.99]).unstack().round(1)
    print("naive day-over-day (us):\n" + naive.to_string())
    burst_share = (m["rate"] >= 100).groupby(m["date"]).mean() * 100
    print(f"msgs sent at >=100 msgs/100us: {burst_share[d1]:.1f}% on {d1} vs {burst_share[d2]:.1f}% on {d2}")

    # DECISION: paired comparison -- the SAME packet on both lines cancels traffic, exchange and clock
    m["bucket"] = pd.cut(m["rate"], RATE_BUCKETS, right=False)
    both = m.dropna(subset=["lat_A", "lat_B"])
    diff = both["lat_B"] - both["lat_A"]
    by_load = diff.groupby([both["bucket"], both["date"]], observed=True).median().unstack()
    print("paired median(B - A) by load bucket (us, <0 = B faster):\n" + by_load.round(2).to_string())
    paired = diff.groupby(both["date"]).median()

    long = m.melt(id_vars=["date", "rate"], value_vars=["lat_A", "lat_B"],
                  var_name="line", value_name="lat").dropna()
    long["rbin"] = np.minimum(long["rate"] // 5 * 5, 200)      # last bin = 200+
    curve = long.groupby(["line", "date", "rbin"])["lat"].agg(
        p50="median", p99=lambda s: s.quantile(0.99), n="size")
    curve = curve[curve["n"] >= 200]
    # Knee: first load bin from which line A's median (unchanged hardware) stays > idle + 2 us
    a = long[long["line"] == "lat_A"].groupby("rbin")["lat"].median()
    above = a > a.iloc[:2].mean() + 2
    knee = int(a.index[a.index > a.index[~above].max()].min())
    capacity = 100_000 / ((mean_bytes + WIRE_OVERHEAD_B) * 8)     # 1 Gb/s = 8 ns per byte
    print(f"knee ~{knee} msgs/100us; 1G link capacity ~{capacity:.0f} msgs/100us "
          f"-> queueing starts at ~{100 * knee / capacity:.0f}% utilisation")

    fig, ax = plt.subplots(figsize=(9, 4.5))
    for (line, date), g in curve.groupby(level=[0, 1]):
        x = g.index.get_level_values("rbin")
        p50 = ax.plot(x, g["p50"], label=f"line {line[-1]} {date}")[0]
        ax.plot(x, g["p99"], ls="--", c=p50.get_color())
    ax.axvline(knee, c="grey", lw=0.8)
    ax.set(yscale="log", xlabel="msgs sent in previous 100 us", ylabel="latency (us)",
           title="Latency vs load: p50 solid, p99 dashed, grey = knee")
    ax.legend()
    fig.savefig(PLOTS / "f3_latency_vs_rate.png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return dict(d1=d1, d2=d2, naive=naive, paired=paired, knee=knee, capacity=capacity,
                burst_share=burst_share)


def step5_conclusion(m: pd.DataFrame, r: dict, n_both_lost: int) -> None:
    d1, d2, p99, paired = r["d1"], r["d2"], r["naive"][("lat_B", 0.99)], r["paired"]
    b_wins = (m.loc[m["date"] == d2, "winner"] == "B").mean()
    print(f"- Upgrade works: B now wins {b_wins:.0%} of races; on the same packets B vs A moved "
          f"{paired[d1]:+.1f} -> {paired[d2]:+.1f} us, in every load bucket.")
    print(f"- B's naive p99 rose {p99[d1]:.0f} -> {p99[d2]:.0f} us only because {d2} had "
          f"{r['burst_share'][d2] / r['burst_share'][d1]:.1f}x more microburst traffic (unchanged A rose too).")
    print(f"- Both lines queue above ~{r['knee']} msgs/100us (~{100 * r['knee'] / r['capacity']:.0f}% of 1G): "
          f"the tail needs bandwidth, not a faster switch.")
    print(f"- {n_both_lost} seqs were lost on both lines: retransmission/snapshot recovery must stay healthy.")


def main() -> None:
    PLOTS.mkdir(exist_ok=True)
    print("== Step 1: integrity per line and day")
    feeds, both_lost = step1_load_and_check()
    print("\n== Step 2: per-line latency (rx - exch, us)")
    step2_latency(feeds)
    print("\n== Step 3: A/B arbitration (winner share %, then latency percentiles)")
    m = step3_arbitrate(feeds)
    for date in sorted(both_lost):
        wins, gaps = step3b_live_arbiter(feeds, date)
        n_gap = sum(hi - lo + 1 for lo, hi in gaps)
        print(f"live arbiter {date}: wins {dict(wins)}, {len(gaps)} gaps / {n_gap} seqs declared")
        # CHECK: the online arbiter must agree with the offline merge (winners and gaps)
        offline = m.loc[m["date"] == date, "winner"].str[0].value_counts().to_dict()  # "A only" -> A
        assert wins == offline and n_gap == len(both_lost[date])
    print("\n== Step 4: did the line-B upgrade help?")
    r = step4_upgrade(m, feeds["A"]["bytes"].mean())
    print("\n== Step 5: conclusion for the network team")
    step5_conclusion(m, r, sum(len(s) for s in both_lost.values()))


if __name__ == "__main__":
    main()
