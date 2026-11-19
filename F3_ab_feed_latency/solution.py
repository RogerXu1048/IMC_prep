"""F3: one multicast feed captured on line A and line B over two days. Did the line-B upgrade help?"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA, PLOTS = [Path(__file__).resolve().parent.parent / p for p in ("data/p04", "plots")]
BEFORE, AFTER = "2026-11-10", "2026-11-11"     # line-B switch upgraded overnight in between
WINDOW_NS = 100_000                             # load = msgs the exchange sent in the previous 100 us
BUCKETS = [0, 25, 50, 75, 100, 150, 1_000]      # load buckets, msgs per 100 us
COLOR, DASH = {"A": "#2a78d6", "B": "#eb6834"}, {BEFORE: "--", AFTER: "-"}
plt.rcParams.update({"savefig.bbox": "tight", "axes.grid": True, "axes.axisbelow": True, "grid.alpha": 0.3})

def load():
    df = pd.concat([pd.read_parquet(DATA / f"feed_{l}.parquet").assign(line=l) for l in "AB"],
                   ignore_index=True)
    df["date"] = df["date"].astype(str)
    df["dup"] = df.duplicated(["line", "date", "seq"])
    # CHECK: rows are in arrival order, so a seq below the running max arrived out of order
    df["ooo"] = (df["seq"] < df.groupby(["line", "date"])["seq"].cummax()) & ~df["dup"]
    print(df.groupby(["line", "date"]).agg(rows=("seq", "size"), dups=("dup", "sum"), ooo=("ooo", "sum")))
    df = df.loc[~df["dup"], ["line", "date", "seq", "exch_ns", "rx_ns"]]    # DECISION: keep 1st copy
    return df.assign(lat_us=(df["rx_ns"] - df["exch_ns"]) / 1e3)

def step1_audit(df):
    # LIMITATION: only gaps between received seqs count; a loss at the very start/end is invisible
    s = df.sort_values(["line", "date", "seq"])
    s["gap"] = s.groupby(["line", "date"])["seq"].diff() - 1      # seqs missing just before this one
    audit = s[s["gap"] > 0].groupby(["line", "date"])["gap"].agg(missing="sum", runs="size").astype(int)
    audit["pct"] = 100 * audit["missing"] / (s.groupby(["line", "date"]).size() + audit["missing"])
    audit["mean_run"] = audit["missing"] / audit["runs"]           # ~1 = random, >1 = clustered
    both = df.groupby("date")["seq"].max() - df.drop_duplicates(["date", "seq"]).groupby("date").size()
    # CHECK: how slow were the packets B lost, measured on line A? Random loss would look typical
    only_a = (df.groupby(["date", "seq"])["line"].transform("size") == 1) & (df["line"] == "A")
    return audit.round(3), both, df.loc[only_a, "lat_us"].median()

def step2_latency(df):
    pct = df.groupby(["line", "date"])["lat_us"].agg(p50="median", p99=lambda s: s.quantile(0.99))
    # CHECK: per-minute MIN strips queueing; same drift on both lines = clock offset, not network
    mins = df.groupby(["line", "date", df["exch_ns"] // 60_000_000_000])["lat_us"].min()
    pct["min_drift_us_per_h"] = mins.groupby(level=[0, 1]).agg(lambda m: 60 * np.polyfit(range(len(m)), m, 1)[0])
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for (line, date), g in df.groupby(["line", "date"]):
        x = np.sort(g["lat_us"].to_numpy())
        ax.plot(x, 100 * (1 - np.arange(len(x)) / len(x)), c=COLOR[line], ls=DASH[date], label=f"{line} {date}")
    ax.set(xscale="log", yscale="log", xlim=(20, 900), ylim=(0.01, 100), xlabel="latency rx - exch (us, log)",
           ylabel="% of packets slower than x (log)", title="Tails got fatter on BOTH lines on busy 11-11")
    ax.set_xticks([20, 50, 100, 200, 500], ["20", "50", "100", "200", "500"])
    ax.set_yticks([100, 10, 1, 0.1, 0.01], ["100%", "10%", "1%", "0.1%", "0.01%"])
    ax.legend(title="line, date")
    fig.savefig(PLOTS / "f3_latency_tail.png")
    return pct.round(2)

def step3_upgrade(df):
    # DECISION: load from the union of both lines, so B's own losses can't hide B's busy periods
    u = df.drop_duplicates(["date", "seq"]).sort_values("exch_ns")
    u["load"] = np.arange(len(u)) - np.searchsorted(u["exch_ns"], u["exch_ns"] - WINDOW_NS, side="right") + 1
    df = df.merge(u[["date", "seq", "load"]], on=["date", "seq"])
    naive = df.groupby(["date", "line"])["lat_us"].quantile(0.99).unstack()
    naive["busy_pct"] = 100 * (u["load"] >= 100).groupby(u["date"]).mean()    # the confounder
    # DECISION: pair the SAME packet on both lines: traffic, exchange and our clock cancel out
    a, b = (df[df["line"] == l] for l in "AB")
    p = a.merge(b[["date", "seq", "exch_ns", "rx_ns"]], on=["date", "seq"], suffixes=("", "_B"))
    assert (p["exch_ns"] == p["exch_ns_B"]).all()          # CHECK: identical packets on both lines
    p["b_minus_a_us"] = (p["rx_ns_B"] - p["rx_ns"]) / 1e3
    paired = p.groupby("date")["b_minus_a_us"].agg(median="median", b_first_pct=lambda s: 100 * (s < 0).mean())
    p["bucket"] = pd.cut(p["load"], BUCKETS, right=False)
    by_load = p.groupby(["bucket", "date"], observed=True)["b_minus_a_us"].median().unstack()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    by_load.plot.bar(ax=ax, rot=0, color=["#9e9e9e", COLOR["B"]], width=0.75)
    ax.axhline(0, color="k", lw=0.8)
    ax.set(xlabel="load bucket (msgs the exchange sent in the previous 100 us)", ylabel="median B - A (us)",
           title="Same packets: B was slower than A before, faster after, at every load")
    fig.savefig(PLOTS / "f3_paired_by_load.png")
    return df, naive.round(1), paired.round(2), by_load.round(2)

def step4_load_effect(df):
    load_bin = df["load"].clip(upper=160) // 10 * 10                    # last bin = 160+
    curve = df.groupby(["line", "date", load_bin])["lat_us"].median().unstack(["line", "date"])
    # DECISION: knee = first load bin whose median is > 3 us above that curve's idle (load < 10)
    knee = (curve > curve.iloc[0] + 3).idxmax()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for (line, date), m in curve.items():
        ax.plot(m.index, m, marker="o", ms=3, c=COLOR[line], ls=DASH[date], label=f"{line} {date}")
    ax.axvspan(knee.min(), knee.max(), color="grey", alpha=0.2, label="knee")
    ax.set(xlabel="load (msgs sent in the previous 100 us; last bin = 160+)", ylabel="median latency (us)",
           title=f"Flat until ~{knee.min()}-{knee.max()} msgs/100us, then queues build on both lines")
    ax.legend(title="line, date")
    fig.savefig(PLOTS / "f3_latency_vs_load.png")
    return knee

def main():
    PLOTS.mkdir(exist_ok=True)
    print("== DATA QUALITY | rows, duplicates, out-of-order arrivals")
    df = load()
    audit, both, lost_b = step1_audit(df)
    print(f"\n== STEP 1 | missing seqs\n{audit}\nlost on BOTH lines: {both.to_dict()}; seqs B lost had "
          f"line-A latency median {lost_b:.0f} us (all packets: {df['lat_us'].median():.0f} us)")
    print(f"\n== STEP 2 | latency rx - exch (us), drift of per-minute minimum\n{step2_latency(df)}")
    df, naive, paired, by_load = step3_upgrade(df)
    print(f"\n== STEP 3 | naive p99 (us); busy_pct = % msgs at >= 100 msgs/100us\n{naive}\n"
          f"paired B - A on the same packet (us)\n{paired}\nmedian B - A by load bucket (us)\n{by_load}")
    knee = step4_load_effect(df)
    print(f"\n== STEP 4 | knee (msgs/100us) per line/day\n{knee.unstack()}")
    d, bf, n = paired["median"], paired["b_first_pct"], naive
    print(f"\nCONCLUSION\n- Upgrade helped: same-packet B - A went {d[BEFORE]:+.1f} -> {d[AFTER]:+.1f} us at "
          f"every load; B arrives first on {bf[BEFORE]:.0f}% -> {bf[AFTER]:.0f}% of packets.")
    print(f"- B's naive p99 rose {n.loc[BEFORE, 'B']} -> {n.loc[AFTER, 'B']} us because {AFTER} was busier "
          f"(busy_pct {n.loc[BEFORE, 'busy_pct']} -> {n.loc[AFTER, 'busy_pct']}); untouched A rose too.")
    print(f"- The tail is load: both lines queue above ~{knee.min()}-{knee.max()} msgs/100us, B drops "
          f"packets in bursts, and {both.sum()} seqs lost on both lines need retransmission.")
    print("\nLIMITATIONS\n- Only two days; load is measured, not controlled (other overnight changes?)."
          "\n- rx - exch mixes two clocks (minimum drifts ~1 us/h on both lines): trust same-packet B - A."
          "\n- Paired stats skip the packets B lost, and those were B's slowest ones (survivor bias).")

if __name__ == "__main__":
    main()
